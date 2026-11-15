"""Micro-pilot driver. Every stage is resumable (all LLM calls are cached). Usage: python3 -m phasef.run_pilot <stage> [...]
Stages: prepare | oracle | full | extract | facts | judge | report"""
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import data
from .facts import build_context, build_extract_prompt, parse_extraction, resolve_all
from .judge import build_batch_prompt, parse_batch
from .llm import LLM
from .prompts import (EXTRACT_RECIPE, EXTRACT_SYSTEM, JUDGE_RECIPE, JUDGE_SYSTEM, READER_RECIPE, READER_SYSTEM,
                      READER_TEMPLATE)

WORK = "/Users/eric/.agent-harness/work/73716864-6b3b-4deb-94ef-8ed2c8432e4a"
DATA = os.path.join(WORK, "data")
OUT = os.path.join(WORK, "phasef-out")
CACHE = os.path.join(OUT, "cache")  # holds benchmark text: work dir only, never in git
NEUTRAL_CWD = os.path.join(OUT, "neutral-cwd")  # not a git repo
READER_MODEL, JUDGE_MODEL = "sonnet", "opus"
EXTRACT_BATCH = 8
FULL_MAX_EST_TOKENS = 185000


def llm(recipe):
    os.makedirs(NEUTRAL_CWD, exist_ok=True)
    return LLM(CACHE, os.path.join(OUT, "ledger_v2.jsonl"), cap=120, concurrency=3, recipe=recipe, cwd=NEUTRAL_CWD)


def pilot():
    return data.load(os.path.join(OUT, "pilot10_s.json")), data.load(os.path.join(OUT, "pilot10_oracle.json"))


def read_prompt(context, rec):
    return READER_TEMPLATE.format(context=context, date=rec["question_date"], question=rec["question"])


def results_path():
    return os.path.join(OUT, "results_v2.jsonl")


def load_results():
    out = {}
    if os.path.exists(results_path()):
        for line in open(results_path()):
            r = json.loads(line)
            out[(r["arm"], r["qid"])] = r
    return out


def save_result(r):
    with open(results_path(), "a") as fh:
        fh.write(json.dumps(r) + "\n")


def run_reader(arm, qid, context, rec, extra=None):
    m = llm(READER_RECIPE)
    c = m.call(read_prompt(context, rec), READER_SYSTEM, READER_MODEL, arm, qid, "read")
    r = {"arm": arm, "qid": qid, "stage": "read", "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"],
         "out_tokens": c["out_tokens"], "seconds": c["seconds"], "cost_usd": c["cost_usd"], "cached": c["cached"], "context_chars": len(context)}
    r.update(extra or {})
    save_result(r)
    return r


def stage_prepare():
    os.makedirs(OUT, exist_ok=True)
    data.write_pilot_files(os.path.join(DATA, "longmemeval_s_cleaned.json"), os.path.join(DATA, "longmemeval_oracle.json"), OUT)
    S, O = pilot()
    for q in data.PILOT_IDS:
        ss = data.sessions_of(S[q])
        txt = data.render_sessions(data.chrono(ss))
        print(q, S[q]["question_type"], "sessions", len(ss), "est_tokens", data.est_tokens(txt))


def stage_oracle():
    S, O = pilot()
    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(run_reader, "ORACLE", q, data.render_sessions(data.chrono(data.sessions_of(O[q]))), S[q]) for q in data.PILOT_IDS]
        for f in futs:
            r = f.result()
            print(r["qid"], r["in_tokens"], r["out_tokens"], r["seconds"], r["hypothesis"][-160:].replace("\n", " "))


def stage_full():
    S, _ = pilot()
    jobs = []
    for q in data.PILOT_IDS:
        ctx = data.render_sessions(data.chrono(data.sessions_of(S[q])))
        est = data.est_tokens(ctx) + data.est_tokens(S[q]["question"]) + 500
        if est > FULL_MAX_EST_TOKENS:
            r = {"arm": "FULL", "qid": q, "stage": "read", "skipped": "does not fit: est %d tokens" % est}
            save_result(r)
            print(q, r["skipped"])
            continue
        jobs.append((q, ctx))
    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(run_reader, "FULL", q, ctx, S[q]) for q, ctx in jobs]
        for f in futs:
            r = f.result()
            print(r["qid"], r["in_tokens"], r["out_tokens"], r["seconds"], r["hypothesis"][-160:].replace("\n", " "))


def _skey(sid, d, turns):
    return "%s|%s|%s" % (sid, d, hashlib.sha256(json.dumps(turns, sort_keys=True).encode()).hexdigest()[:16])


def fact_sessions(S):
    uniq, refs = {}, 0
    for q in data.FACT_QIDS:
        for sid, d, turns in data.sessions_of(S[q]):
            refs += 1
            uniq.setdefault(_skey(sid, d, turns), (sid, d, turns))
    return uniq, refs


def stage_extract(limit=None):
    S, _ = pilot()
    uniq, refs = fact_sessions(S)
    keys = sorted(uniq)
    batches = [[uniq[k] for k in keys[i:i + EXTRACT_BATCH]] for i in range(0, len(keys), EXTRACT_BATCH)]
    print("session refs", refs, "unique", len(uniq), "batches", len(batches))
    batches = batches[:int(limit)] if limit else batches
    m = llm(EXTRACT_RECIPE)
    store = os.path.join(OUT, "facts_raw.jsonl")

    def one(i):
        b = batches[i]
        c = m.call(build_extract_prompt(b), EXTRACT_SYSTEM, READER_MODEL, "FACTS", "batch%02d" % i, "extract")
        try:
            per, dropped = parse_extraction(c["result"], b)
        except Exception as e:  # recorded, not retried silently: a retry costs a call
            return i, None, str(e), c
        return i, per, dropped, c

    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(one, range(len(batches))))
    bad = [(i, d) for i, per, d, c in res if per is None]
    with open(store, "w") as fh:
        for (i, per, d, c) in res:
            if per is None:
                continue
            for item, fs in zip(batches[i], per):
                for f in fs:
                    g = dict(f)
                    g["valid_from"], g["asserted_at"] = f["valid_from"].isoformat(), f["asserted_at"].isoformat()
                    g["session_key"] = _skey(*item)
                    fh.write(json.dumps(g) + "\n")
    print("parse failures", bad)
    for i, per, d, c in res:
        print(i, "facts", sum(len(v) for v in per) if per else None, "in", c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out", c["out_tokens"], "s", c["seconds"])


def load_facts(S):
    from datetime import datetime
    per_key = {}
    for line in open(os.path.join(OUT, "facts_raw.jsonl")):
        f = json.loads(line)
        f["valid_from"], f["asserted_at"] = datetime.fromisoformat(f["valid_from"]), datetime.fromisoformat(f["asserted_at"])
        per_key.setdefault(f["session_key"], []).append(f)
    return per_key


def resolved_for(S, q, per_key):
    ss = data.chrono(data.sessions_of(S[q]))
    per = {}
    for sid, d, turns in ss:
        per[sid] = per_key.get(_skey(sid, d, turns), [])
    return resolve_all(per, [s[0] for s in ss]), {s[0]: (s[1], s[2]) for s in ss}


def stage_facts():
    S, _ = pilot()
    per_key = load_facts(S)
    jobs = []
    for q in data.FACT_QIDS:
        facts, by_id = resolved_for(S, q, per_key)
        from emlab.dataset import parse_date
        ctx, info = build_context(S[q]["question"], parse_date(S[q]["question_date"]), facts, by_id)
        jobs.append((q, ctx, info, len(facts)))
    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(run_reader, "FACTS", q, ctx, S[q], dict(info, facts_total=n)) for q, ctx, info, n in jobs]
        for f in futs:
            r = f.result()
            print(r["qid"], r["in_tokens"], r["out_tokens"], r["facts_shown"], r["rounds_cited"], r["hypothesis"][-160:].replace("\n", " "))


def stage_judge(batch=6):
    S, _ = pilot()
    res = load_results()
    todo = [r for r in res.values() if "hypothesis" in r and "label" not in r]
    todo.sort(key=lambda r: (r["arm"], r["qid"]))
    m = llm(JUDGE_RECIPE)
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def one(ch):
        items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in ch]
        c = m.call(build_batch_prompt(items), JUDGE_SYSTEM, JUDGE_MODEL, "JUDGE", "+".join(r["qid"] for r in ch)[:60], "judge")
        return ch, parse_batch(c["result"], len(ch)), c

    with ThreadPoolExecutor(3) as ex:
        out = list(ex.map(one, chunks))
    for ch, labels, c in out:
        for r, lab in zip(ch, labels):
            r2 = dict(r, label=lab, judge_in=(c["in_tokens"] + c["cache_creation"] + c["cache_read"]) / float(len(ch)),
                      judge_out=c["out_tokens"] / float(len(ch)), judge_seconds=c["seconds"] / len(ch))
            save_result(r2)
            print(r["arm"], r["qid"], lab)


if __name__ == "__main__":
    for st in sys.argv[1:]:
        name, _, arg = st.partition(":")
        globals()["stage_" + name](*([arg] if arg else []))
