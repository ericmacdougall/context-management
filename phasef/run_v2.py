"""Recipe v2 re-run of the FACTS arm on the same 4 questions. Hard ceiling for this round: counter 102 (62 used before, at most 40 more).
Stages: overhead | extract | expand | read | judge | analyse   Usage: python3 -m phasef.run_v2 <stage> ..."""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import data, run_pilot as R
from .facts_v2 import build_context_v2, build_extract_prompt_v2, parse_extraction_v2
from .judge import build_batch_prompt, parse_batch
from .llm import LLM
from .prompts import (EXPAND_RECIPE, EXPAND_SYSTEM, EXPAND_TEMPLATE, EXTRACT_SYSTEM, EXTRACT_V2_RECIPE, JUDGE_RECIPE, JUDGE_SYSTEM,
                      READER_RECIPE, READER_SYSTEM)
from .supersede_v2 import resolve_v2, slot_match_stats

CEILING = 102
RES = os.path.join(R.OUT, "results_v3.jsonl")
FACTS2 = os.path.join(R.OUT, "facts_v2_raw.jsonl")
CUES = os.path.join(R.OUT, "question_cues.json")


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    return LLM(R.CACHE, os.path.join(R.OUT, "ledger_v2.jsonl"), cap=CEILING, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD)


def stage_overhead():
    c = llm("overhead-v1").call("Reply with the single word: ok", "You are terse.", "sonnet", "OVERHEAD", "none", "overhead")
    print({k: c[k] for k in ("in_tokens", "cache_creation", "cache_read", "out_tokens", "result")})


def stage_extract():
    S, _ = R.pilot()
    uniq, refs = R.fact_sessions(S)
    keys = sorted(uniq)
    batches = [[uniq[k] for k in keys[i:i + R.EXTRACT_BATCH]] for i in range(0, len(keys), R.EXTRACT_BATCH)]
    m = llm(EXTRACT_V2_RECIPE)

    def one(i):
        b = batches[i]
        c = m.call(build_extract_prompt_v2(b), EXTRACT_SYSTEM, "sonnet", "FACTS2", "batch%02d" % i, "extract")
        try:
            return i, parse_extraction_v2(c["result"], b), c
        except Exception as e:
            return i, (None, str(e)), c

    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(one, range(len(batches))))
    with open(FACTS2, "w") as fh:
        for i, (per, d), c in res:
            if per is None:
                print("PARSE FAIL batch", i, d)
                continue
            for item, fs in zip(batches[i], per):
                for f in fs:
                    g = dict(f)
                    g["asserted_at"], g["valid_from"] = f["asserted_at"].isoformat(), f["valid_from"].isoformat()
                    g["session_key"] = R._skey(*item)
                    fh.write(json.dumps(g) + "\n")
    for i, (per, d), c in res:
        print(i, "facts", sum(len(v) for v in per) if per else None, "in", c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out", c["out_tokens"])


def stage_expand():
    S, _ = R.pilot()
    m = llm(EXPAND_RECIPE)
    out = {}
    for q in data.FACT_QIDS:
        c = m.call(EXPAND_TEMPLATE.format(date=S[q]["question_date"], question=S[q]["question"]), EXPAND_SYSTEM, "sonnet", "FACTS2", q, "expand")
        t = c["result"]
        out[q] = json.loads(t[t.find("{"):t.rfind("}") + 1])["cue_keys"]
        print(q, out[q])
    json.dump(out, open(CUES, "w"))


def load_v2():
    per = {}
    for line in open(FACTS2):
        f = json.loads(line)
        f["asserted_at"], f["valid_from"] = datetime.fromisoformat(f["asserted_at"]), datetime.fromisoformat(f["valid_from"])
        per.setdefault(f["session_key"], []).append(f)
    return per


def resolved(S, q, per):
    ss = data.chrono(data.sessions_of(S[q]))
    flat, order = [], 0
    for sid, d, turns in ss:
        for f in per.get(R._skey(sid, d, turns), []):
            g = dict(f)
            g["order"] = order
            order += 1
            flat.append(g)
    return resolve_v2(flat), {s[0]: (s[1], s[2]) for s in ss}


def save(r):
    with open(RES, "a") as fh:
        fh.write(json.dumps(r) + "\n")


def stage_read():
    from emlab.dataset import parse_date
    S, _ = R.pilot()
    per, cues = load_v2(), json.load(open(CUES))
    m = llm(READER_RECIPE)

    def one(q):
        facts, by = resolved(S, q, per)
        ctx, info = build_context_v2(S[q]["question"], cues[q], parse_date(S[q]["question_date"]), facts, by)
        c = m.call(R.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "FACTS2", q, "read")
        r = {"arm": "FACTS2", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"],
             "out_tokens": c["out_tokens"], "facts_shown": info["facts_shown"], "rounds_cited": info["rounds_cited"]}
        save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, data.FACT_QIDS):
            print(r["qid"], r["in_tokens"], r["hypothesis"][-140:].replace("\n", " "))


def stage_judge():
    S, _ = R.pilot()
    rs = {}
    for l in open(RES):
        r = json.loads(l)
        rs[r["qid"]] = r
    todo = [rs[q] for q in data.FACT_QIDS if "label" not in rs[q]]
    items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in todo]
    c = llm(JUDGE_RECIPE).call(build_batch_prompt(items), JUDGE_SYSTEM, "opus", "JUDGE", "facts2", "judge")
    for r, lab in zip(todo, parse_batch(c["result"], len(todo))):
        save(dict(r, label=lab))
        print(r["qid"], lab)


def stage_analyse():
    from emlab.dataset import parse_date
    S, _ = R.pilot()
    per, cues = load_v2(), json.load(open(CUES))
    for q in data.FACT_QIDS:
        facts, by = resolved(S, q, per)
        sup = [f for f in facts if f["status"] == "superseded"]
        print("==", q, "facts", len(facts), "superseded", len(sup), slot_match_stats(facts))
        ctx, info = build_context_v2(S[q]["question"], cues[q], parse_date(S[q]["question_date"]), facts, by)
        gold = [f for f in info["top"] if f["session_id"].startswith("answer_")]
        print("  gold-session facts in the shown top-%d: %d" % (info["facts_shown"], len(gold)))
        if q in ("07741c44", "07741c45", "6e984302"):
            for f in facts:
                if f["session_id"].startswith("answer_") and f["modality"] in ("asserted", "decided"):
                    print("   G", f["status"], f["cardinality"], f["slot"], "->", f["slot_norm"], "|", f["object"][:50])
        if q == "07741c45":
            for f in sup:
                y = facts[f["superseded_by"]]
                print("   S %s [%s] %s  -->  %s [%s] %s" % (f["slot"], f["session_id"][-8:], f["object"][:40], y["slot"], y["session_id"][-8:], y["object"][:40]))


if __name__ == "__main__":
    for st in sys.argv[1:]:
        globals()["stage_" + st]()
