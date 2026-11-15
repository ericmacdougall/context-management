"""Re-test with opaque session ids (no model sees a real id; llm.py guard enforces it). Stages: oracle full extract2 facts judge report.
Separate counter (cache dir phasef-out/anon/cache, cap 160). Old results are read from the earlier result files for side-by-side comparison."""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import data, run_pilot as R, run_v3 as V3
from .facts_v2 import build_extract_prompt_v2, parse_extraction_v2
from .judge import build_batch_prompt, parse_batch
from .llm import LLM
from .prompts import (EXTRACT_SYSTEM, EXTRACT_V2_RECIPE, JUDGE_RECIPE, JUDGE_SYSTEM, READER_RECIPE, READER_SYSTEM, READER_V12_RECIPE)

OUT = R.OUT
AN = os.path.join(OUT, "anon")
CACHE = os.path.join(AN, "cache")
RES = os.path.join(AN, "results_anon.jsonl")
CAP = 160


def real_ids(*files):
    ids = set()
    for p in files:
        for rec in json.load(open(p)):
            ids.update(rec["haystack_session_ids"])
    return ids


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    forb = real_ids(os.path.join(OUT, "pilot10_s.json"), os.path.join(OUT, "gate16_s.json"), os.path.join(OUT, "pilot10_oracle.json"), os.path.join(OUT, "gate16_oracle.json"))
    return LLM(CACHE, os.path.join(AN, "ledger.jsonl"), cap=CAP, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD, forbidden=forb)


def save(r):
    with open(RES, "a") as fh:
        fh.write(json.dumps(r) + "\n")


def last():
    out = {}
    if os.path.exists(RES):
        for l in open(RES):
            r = json.loads(l)
            out.setdefault((r["arm"], r["qid"]), {}).update(r)
    return out


def stage_oracle(which="ORACLE"):
    S, O = R.pilot()
    m = llm(READER_RECIPE)

    def one(q):
        src = O if which == "ORACLE" else S
        ctx = data.render_sessions(data.chrono(data.sessions_of(src[q])))
        c = m.call(R.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", which + "-A", q, "read")
        r = {"arm": which + "-A", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
        save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, data.PILOT_IDS):
            print(r["arm"], r["qid"], r["in_tokens"], r["hypothesis"][-80:].replace("\n", " "))


def stage_full():
    stage_oracle("FULL")


def stage_extract2():
    """Re-extract the sessions of 07741c44 and 07741c45 with opaque ids at 8 per call (same batch size, same prompt otherwise)."""
    S, _ = V3.gate()
    per_old = {}
    for l in open(os.path.join(OUT, "facts_v2_raw.jsonl")):
        f = json.loads(l)
        per_old.setdefault(f["session_key"], []).append(f)
    uniq = {}
    for q in ("07741c44", "07741c45"):
        for sid, d, turns in data.sessions_of(S[q]):
            uniq.setdefault(R._skey(sid, d, turns), (sid, d, turns))
    keys = sorted(uniq)
    batches = [[uniq[k] for k in keys[i:i + 8]] for i in range(0, len(keys), 8)]
    m = llm(EXTRACT_V2_RECIPE)

    def one(b):
        c = m.call(build_extract_prompt_v2(b), EXTRACT_SYSTEM, "sonnet", "REEXTRACT", b[0][0], "extract")
        per, dropped = parse_extraction_v2(c["result"], b)
        return b, per, dropped
    new = {}
    with ThreadPoolExecutor(3) as ex:
        for b, per, dropped in ex.map(one, batches):
            for item, fs in zip(b, per):
                new[R._skey(*item)] = fs
    tok = lambda f: set(re.findall(r"[a-z0-9]+", ("%s %s" % (f["predicate"], f["object"])).lower()))
    rows = []
    for k in keys:
        old, nw = per_old.get(k, []), new.get(k, [])
        matched = 0
        for f in old:
            tf = tok(f)
            if any(len(tf & tok(g)) / float(len(tf | tok(g)) or 1) >= 0.4 for g in nw):
                matched += 1
        rows.append((k.startswith("answer_"), len(old), len(nw), matched))
    for name, sel in (("gold (answer_) sessions", [r for r in rows if r[0]]), ("other sessions", [r for r in rows if not r[0]])):
        n = max(1, len(sel))
        print("%s: n=%d old facts/session %.2f new (opaque) %.2f; share of old facts with a lexical match (jaccard >= 0.4) in the new list %.1f%%" % (
            name, len(sel), sum(r[1] for r in sel) / float(n), sum(r[2] for r in sel) / float(n), 100.0 * sum(r[3] for r in sel) / max(1, sum(r[1] for r in sel))))
    print("all: old %d facts, new %d facts over %d sessions" % (sum(r[1] for r in rows), sum(r[2] for r in rows), len(rows)))
    json.dump(rows, open(os.path.join(AN, "reextract.json"), "w"))


def stage_facts():
    S, _ = V3.gate()
    per, canon, cues = V3.load_facts(), json.load(open(V3.CANON)), json.load(open(V3.CUES))
    m = llm(READER_V12_RECIPE)

    def one(args):
        q, labels = args
        facts, by, links = V3.resolved(S, q, per, canon)
        ctx, top = V3.build_context_v3(S[q]["question"], cues[q], data.parse_date(S[q]["question_date"]), facts, by, labels=labels)
        arm = "FACTS-A" if labels else "NOLABEL-A"
        c = m.call(V3.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", arm, q, "read")
        r = {"arm": arm, "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
        save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, [(q, lab) for q in V3.ids() for lab in (True, False)]):
            print(r["arm"], r["qid"], r["in_tokens"], r["hypothesis"][-70:].replace("\n", " "))


def stage_judge(batch=6):
    S1, _ = R.pilot()
    S2, _ = V3.gate()
    S = dict(S1)
    S.update(S2)
    res = last()
    todo = sorted((r for r in res.values() if "hypothesis" in r and "label" not in r), key=lambda r: (r["arm"], r["qid"]))
    m = llm(JUDGE_RECIPE)
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def one(ch):
        items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in ch]
        c = m.call(build_batch_prompt(items), JUDGE_SYSTEM, "opus", "JUDGE", "anon" + ch[0]["arm"] + ch[0]["qid"], "judge")
        return ch, parse_batch(c["result"], len(ch))
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            for r, lab in zip(ch, labs):
                save(dict(r, label=lab))
    print("judged", len(todo))


def old_labels():
    out = {}
    for l in open(os.path.join(OUT, "results_v2.jsonl")):
        r = json.loads(l)
        if "label" in r:
            out[(r["arm"], r["qid"])] = r["label"]
    for l in open(os.path.join(OUT, "results_v4.jsonl")):
        r = json.loads(l)
        if "label" in r:
            out[(r["arm"], r["qid"])] = r["label"]
    for l in open(os.path.join(OUT, "rescue", "results_nolabel.jsonl")):
        r = json.loads(l)
        out[("NOLABEL", r["qid"])] = r["label"]
    return out


def stage_report():
    old, new = old_labels(), {k: v["label"] for k, v in last().items() if "label" in v}
    for a_old, a_new, ids in (("ORACLE", "ORACLE-A", data.PILOT_IDS), ("FULL", "FULL-A", data.PILOT_IDS), ("FACTS3", "FACTS-A", V3.ids()), ("NOLABEL", "NOLABEL-A", V3.ids())):
        o = [old.get((a_old, q)) for q in ids]
        n = [new.get((a_new, q)) for q in ids]
        print("%s old %s/%d  new %s/%d" % (a_old, sum(1 for x in o if x), len(ids), sum(1 for x in n if x), len(ids)))
        print("   per question (old -> new):", {q: "%s->%s" % ({True: "ok", False: "x", None: "-"}[x], {True: "ok", False: "x", None: "-"}[y]) for q, x, y in zip(ids, o, n)})


if __name__ == "__main__":
    os.makedirs(AN, exist_ok=True)
    for st in sys.argv[1:]:
        globals()["stage_" + st]()
