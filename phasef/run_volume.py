"""Staged volume extraction (recipe extract-v2 unchanged, 16 sessions per call). Resumable: every call is cached.
Stages: index plan quality extract assemble   Usage: python3 -u -m phasef.run_volume <stage> ...
Dates: a session's date differs between questions, so the prompt uses the EARLIEST date the session carries in the dataset (deterministic);
facts carry the session id only and event_time resolved against that date is a known limitation."""
import hashlib
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import data, run_pilot as R
from .facts_v2 import build_extract_prompt_v2, parse_extraction_v2
from .llm import LLM
from .prompts import EXTRACT_SYSTEM, EXTRACT_V2_RECIPE
from .run_v3 import strict_ok

BASE = R.WORK
OUT = os.path.join(BASE, "phasef-out")
VOL = os.path.join(OUT, "volume")
INDEX = os.path.join(VOL, "sessions_needed.json")
POOL = os.path.join(VOL, "pool_sets_pilot.json")
CACHE = os.path.join(VOL, "cache")
CACHE_Q = os.path.join(VOL, "cache_q")
CACHE_EXT = os.path.join(VOL, "cache_ext")  # extraction counter, cap 580
CACHE_C = os.path.join(VOL, "cache_curve")  # batch-size curve counter, cap 45
RAW_VOL = os.path.join(VOL, "facts_vol_raw.jsonl")
CAP_VOL, CAP_Q = 580, 12
BATCH = 16


def llm(cache, cap, ledger):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    return LLM(cache, os.path.join(VOL, ledger), cap=cap, concurrency=3, recipe=EXTRACT_V2_RECIPE, cwd=R.NEUTRAL_CWD)


def chash(turns):
    return hashlib.sha256(json.dumps(turns, sort_keys=True).encode()).hexdigest()[:16]


def stage_index():
    from emlab.dataset import LongMemEvalLoader, select_split
    path = os.path.join(R.DATA, "longmemeval_s_cleaned.json")
    qs = list(LongMemEvalLoader(path).questions())
    pilot = set(q.qid for q in select_split(qs, "pilot"))
    del qs
    pool = json.load(open(POOL))
    d500 = set(pool["D500"])
    need_pilot, sess = set(), {}
    with open(path, "r", encoding="utf-8") as fh:
        recs = json.load(fh)
    for rec in recs:
        isp = str(rec["question_id"]) in pilot
        for sid, d, s in zip(rec["haystack_session_ids"], rec["haystack_dates"], rec["haystack_sessions"]):
            if isp:
                need_pilot.add(sid)
            sess.setdefault(sid, []).append((data.parse_date(d), d, s))  # session content and dates only
    del recs
    need = need_pilot | d500
    missing = [s for s in d500 if s not in sess]
    out = {}
    for sid in sorted(need):
        dt, d, turns = min(sess[sid], key=lambda x: x[0])
        out[sid] = {"date": d, "turns": turns, "hash": chash(turns), "pilot": sid in need_pilot, "d500": sid in d500}
    json.dump(out, open(INDEX, "w"))
    print("pilot questions", len(pilot), "pilot-haystack unique sessions", len(need_pilot), "D500", len(d500), "D500 missing from dataset", len(missing),
          "union", len(need), "overlap pilot&D500", len(need_pilot & d500))


def load_index():
    return json.load(open(INDEX))


def raw_lines(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def cached_extractions():
    """(sid, content hash) -> list of raw fact dicts, from the dev v2/v3 extractions, the rescue dev2 extraction and this volume run."""
    out = {}
    for path in (os.path.join(OUT, "facts_v2_raw.jsonl"), os.path.join(OUT, "facts_v3_raw.jsonl"), os.path.join(OUT, "rescue", "facts_dev2_raw.jsonl"), RAW_VOL):
        for f in raw_lines(path):
            sid, _, h = f["session_key"].split("|")
            out.setdefault((sid, h), []).append(f)
    # sessions extracted with zero facts leave no row; they are re-extracted (counted)
    return out


def todo_sessions(idx):
    have = cached_extractions()
    return [sid for sid in sorted(idx) if (sid, idx[sid]["hash"]) not in have]


def stage_plan():
    idx = load_index()
    have = cached_extractions()
    todo = todo_sessions(idx)
    calls = -(-len(todo) // BATCH)
    chars = sum(len(json.dumps(idx[s]["turns"])) for s in todo)
    est_in = len(todo) * 3600 + calls * 10500
    est_out = len(todo) * 700
    line = "VOLUME PLAN: sessions needed %d (pilot %d, D500 %d), already cached %d, to extract %d, calls at %d per call %d, estimated input %.1fM tokens, output %.1fM; limits 360 calls / 26M input" % (
        len(idx), sum(1 for v in idx.values() if v["pilot"]), sum(1 for v in idx.values() if v["d500"]), len(idx) - len(todo), len(todo), BATCH, calls, est_in / 1e6, est_out / 1e6)
    print(line)
    with open(os.path.join(BASE, "elastic-memory-lab-phasef", "phasef", "DEVLOG.md"), "a") as fh:
        fh.write("\n" + line + " (printed before the run)\n")
    print("ok to start:", calls <= CAP_VOL and est_in <= 26e6)


def stage_quality():
    """48 sessions already extracted at batch 8 (v2 run on the 4 questions), re-extracted at batch 16; Opus compares the fact lists."""
    idx = load_index()
    v2 = {}
    for f in raw_lines(os.path.join(OUT, "facts_v2_raw.jsonl")):
        sid, _, h = f["session_key"].split("|")
        v2.setdefault((sid, h), []).append(f)
    keys = sorted(k for k in v2 if k[0] in idx and idx[k[0]]["hash"] == k[1])
    sample = random.Random(16096).sample(keys, 48)
    sample.sort()
    m = llm(CACHE_Q, CAP_Q, "ledger_quality.jsonl")
    batches = [sample[i:i + 16] for i in range(0, 48, 16)]
    f16 = {}
    for bi, b in enumerate(batches):
        batch = [(sid, idx[sid]["date"], idx[sid]["turns"]) for sid, h in b]
        c = m.call(build_extract_prompt_v2(batch), EXTRACT_SYSTEM, "sonnet", "QUALITY", "q16_%d" % bi, "extract")
        per, dropped = parse_extraction_v2(c["result"], batch)
        print("batch16 sample", bi, "strict", strict_ok(c["result"]), "facts", sum(len(x) for x in per), "dropped", dropped, "out", c["out_tokens"])
        for (sid, h), fs in zip(b, per):
            f16[(sid, h)] = fs
    n8 = sum(len(v2[k]) for k in sample)
    n16 = sum(len(f16[k]) for k in sample)
    print("facts per session: batch8 %.2f batch16 %.2f (48 sessions, totals %d vs %d); change %+.1f%%" % (n8 / 48.0, n16 / 48.0, n8, n16, 100.0 * (n16 - n8) / n8))
    # Opus comparison, 8 sessions per call
    stmt = lambda f: "%s | %s = %s" % (f["subject"], f["predicate"], f["object"])
    only8 = only16 = both = 0
    jm = llm(CACHE_Q, CAP_Q, "ledger_quality.jsonl")
    for ci in range(0, 48, 8):
        ks = sample[ci:ci + 8]
        blocks = []
        for j, k in enumerate(ks, 1):
            blocks.append("=== SESSION %d ===\nList A (%d facts):\n%s\nList B (%d facts):\n%s\n=== END SESSION %d ===" % (
                j, len(v2[k]), "\n".join("A%d. %s" % (i + 1, stmt(f)) for i, f in enumerate(v2[k])), len(f16[k]), "\n".join("B%d. %s" % (i + 1, stmt(f)) for i, f in enumerate(f16[k])), j))
        prompt = ("For each session below, two memory systems extracted facts from the same chat session into List A and List B. A fact in one list is MATCHED if the other list contains a fact that states the same information "
                  "(wording may differ; a fact that merges or splits others still counts as matched for the information it covers). Count: onlyA = facts in A with no match in B; onlyB = facts in B with no match in A; both = facts in A that are matched in B.\n\n"
                  + "\n\n".join(blocks) + "\n\nOutput exactly one line per session: '<number>: onlyA=<n> onlyB=<n> both=<n>' and nothing else.")
        c = jm.call(prompt, "You are a careful comparer. Output only the requested lines.", "opus", "QUALITY", "cmp%d" % ci, "compare")
        import re
        got = {int(m_.group(1)): tuple(int(x) for x in m_.groups()[1:]) for m_ in re.finditer(r"^\s*(\d+):\s*onlyA=(\d+)\s+onlyB=(\d+)\s+both=(\d+)", c["result"], re.M)}
        for j in range(1, len(ks) + 1):
            a, b_, bo = got[j]
            only8 += a
            only16 += b_
            both += bo
    print("Opus comparison over 48 sessions: facts only in batch-8 list %d, only in batch-16 list %d, matched %d; batch 16 drops %.1f%% of batch-8 facts, adds %.1f%%" % (
        only8, only16, both, 100.0 * only8 / n8, 100.0 * only16 / n16))
    json.dump({"n8": n8, "n16": n16, "only8": only8, "only16": only16, "both": both}, open(os.path.join(VOL, "quality_sample.json"), "w"))


def stage_curve():
    """Batch-size curve on 24 of the 48 sample sessions: 1, 4 per call (new), 8 (cached v2 run), 16 (cached quality run); Opus matches each size against 1 per call."""
    import re
    idx = load_index()
    v2 = {}
    for f in raw_lines(os.path.join(OUT, "facts_v2_raw.jsonl")):
        sid, _, h = f["session_key"].split("|")
        v2.setdefault((sid, h), []).append(f)
    keys = sorted(k for k in v2 if k[0] in idx and idx[k[0]]["hash"] == k[1])
    sample48 = random.Random(16096).sample(keys, 48)
    s24 = sorted(sample48[:24])
    mq = llm(CACHE_Q, CAP_Q, "ledger_quality.jsonl")  # cache hits only (same prompts as the quality stage): free
    s48 = sorted(sample48)
    f16 = {}
    for bi in range(3):
        b = s48[bi * 16:(bi + 1) * 16]
        batch = [(sid, idx[sid]["date"], idx[sid]["turns"]) for sid, h in b]
        c = mq.call(build_extract_prompt_v2(batch), EXTRACT_SYSTEM, "sonnet", "QUALITY", "q16_%d" % bi, "extract")
        per, _ = parse_extraction_v2(c["result"], batch)
        for k, fs in zip(b, per):
            f16[k] = fs
    m = llm(CACHE_C, 45, "ledger_curve.jsonl")
    lists = {8: {k: v2[k] for k in s24}, 16: {k: f16[k] for k in s24}, 4: {}, 1: {}}
    for size in (4, 1):
        chunks = [s24[i:i + size] for i in range(0, 24, size)]

        def one(b, size=size):
            batch = [(sid, idx[sid]["date"], idx[sid]["turns"]) for sid, h in b]
            c = m.call(build_extract_prompt_v2(batch), EXTRACT_SYSTEM, "sonnet", "CURVE", "c%d_%s" % (size, b[0][0]), "extract")
            per, dropped = parse_extraction_v2(c["result"], batch)
            return b, per, c
        with ThreadPoolExecutor(3) as ex:
            for b, per, c in ex.map(one, chunks):
                for k, fs in zip(b, per):
                    lists[size][k] = fs
    stmt = lambda f: "%s | %s = %s" % (f["subject"], f["predicate"], f["object"])
    res = {}
    for size in (1, 4, 8, 16):
        print("facts per session at %d per call: %.2f (24 sessions, %d facts)" % (size, sum(len(v) for v in lists[size].values()) / 24.0, sum(len(v) for v in lists[size].values())))
    for size in (4, 8, 16):
        onlyA = onlyB = both = trA = trB = 0
        for ci in range(0, 24, 8):
            ks = s24[ci:ci + 8]
            blocks = []
            for j, k in enumerate(ks, 1):
                A, B = lists[1][k], lists[size][k]
                blocks.append("=== SESSION %d ===\nList A (%d facts):\n%s\nList B (%d facts):\n%s\n=== END SESSION %d ===" % (
                    j, len(A), "\n".join("A%d. %s" % (i + 1, stmt(f)) for i, f in enumerate(A)), len(B), "\n".join("B%d. %s" % (i + 1, stmt(f)) for i, f in enumerate(B)), j))
            prompt = ("For each session below, two memory systems extracted facts from the same chat session into List A and List B. A fact in one list is MATCHED if the other list contains a fact that states the same information "
                      "(wording may differ; a merged or split fact counts as matched for the information it covers). Count: onlyA = facts in A with no match in B; onlyB = facts in B with no match in A; both = facts in A matched in B; "
                      "trivialA = facts in A that are trivial (small talk, generic filler, something nobody would need to remember later) or duplicate another fact in A; trivialB = the same for B.\n\n"
                      + "\n\n".join(blocks) + "\n\nOutput exactly one line per session: '<number>: onlyA=<n> onlyB=<n> both=<n> trivialA=<n> trivialB=<n>' and nothing else.")
            c = m.call(prompt, "You are a careful comparer. Output only the requested lines.", "opus", "CURVE", "cmp%d_%d" % (size, ci), "compare")
            got = {int(x.group(1)): tuple(int(y) for y in x.groups()[1:]) for x in re.finditer(r"^\s*(\d+):\s*onlyA=(\d+)\s+onlyB=(\d+)\s+both=(\d+)\s+trivialA=(\d+)\s+trivialB=(\d+)", c["result"], re.M)}
            for j in range(1, len(ks) + 1):
                a, b_, bo, ta, tb = got[j]
                onlyA += a
                onlyB += b_
                both += bo
                trA += ta
                trB += tb
        nA = sum(len(lists[1][k]) for k in s24)
        nB = sum(len(lists[size][k]) for k in s24)
        res[size] = {"nA": nA, "nB": nB, "onlyA": onlyA, "onlyB": onlyB, "both": both, "trivialA": trA, "trivialB": trB}
        print("size %2d vs 1: 1-per-call facts missing at this size %d of %d (%.1f%%); facts only at this size %d (%.1f%% of its %d); trivial/duplicate: 1-per-call list %d (%.1f%%), this size %d (%.1f%%)" % (
            size, onlyA, nA, 100.0 * onlyA / nA, onlyB, 100.0 * onlyB / nB, nB, trA, 100.0 * trA / nA, trB, 100.0 * trB / nB))
    json.dump(res, open(os.path.join(VOL, "curve.json"), "w"))


def stage_extract():
    idx = load_index()
    todo = todo_sessions(idx)
    m = llm(CACHE_EXT, CAP_VOL, "ledger_volume.jsonl")
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print("sessions", len(todo), "batches", len(batches), flush=True)

    def run(sids, tag):
        batch = [(s, idx[s]["date"], idx[s]["turns"]) for s in sids]
        c = m.call(build_extract_prompt_v2(batch), EXTRACT_SYSTEM, "sonnet", "VOLUME", tag, "extract")
        try:
            per, dropped = parse_extraction_v2(c["result"], batch)
        except Exception as e:
            return None, str(e), c, batch
        import re
        ok = strict_ok(c["result"]) and len(re.findall(r'"session_id":\s*"', c["result"])) == len(batch)
        return (per if ok else None), ("incomplete" if not ok else dropped), c, batch

    def one(i):
        sids = batches[i]
        per, info, c, batch = run(sids, "v16_%04d" % i)
        if per is not None:
            return [(batch, per)], [], 1
        done, fails, n = [], [], 1
        for h in (0, 1):  # retry once at 8 sessions per call
            sub = sids[h * 8:(h + 1) * 8]
            if not sub:
                continue
            try:
                per2, info2, c2, b2 = run(sub, "v8r_%04d_%d" % (i, h))
            except Exception as e:
                fails.append((sub, "call failed: %s" % e))
                continue
            n += 1
            if per2 is None:
                fails.append((sub, "retry failed: %s" % info2))
            else:
                done.append((b2, per2))
        return done, fails, n

    results, fails, calls = [], [], 0
    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(one, i) for i in range(len(batches))]
        for k, f in enumerate(futs):
            try:
                d, fl, n = f.result()
            except Exception as e:
                fails.append(("batch %d" % k, "exception %s" % e))
                continue
            results += d
            fails += fl
            calls += n
            if k % 20 == 0:
                print("progress", k, "of", len(batches), "calls so far", calls, flush=True)
    with open(RAW_VOL, "w") as fh:
        for batch, per in results:
            for (sid, d, turns), fs in zip(batch, per):
                for f in fs:
                    g = dict(f)
                    g["asserted_at"], g["valid_from"] = f["asserted_at"].isoformat(), f["valid_from"].isoformat()
                    g["session_key"] = "%s|%s|%s" % (sid, d, chash(turns))
                    fh.write(json.dumps(g) + "\n")
    json.dump({"failures": fails}, open(os.path.join(VOL, "failures.json"), "w"))
    print("EXTRACT DONE calls (this process)", calls, "failures", len(fails), "counter", m.calls_used())


def stage_cues():
    """Question cue keys for the 102 pilot questions: unchanged expand-v1 prompt, sees only question text and date."""
    from emlab.dataset import LongMemEvalLoader, select_split
    from .prompts import EXPAND_RECIPE, EXPAND_SYSTEM, EXPAND_TEMPLATE
    path = os.path.join(R.DATA, "longmemeval_s_cleaned.json")
    qs = list(LongMemEvalLoader(path).questions())
    pilot = sorted(q.qid for q in select_split(qs, "pilot"))
    del qs
    recs = data.extract_records(path, pilot)
    recs.sort(key=lambda r: str(r["question_id"]))
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    m = LLM(CACHE, os.path.join(VOL, "ledger_cues.jsonl"), cap=470, concurrency=3, recipe=EXPAND_RECIPE, cwd=R.NEUTRAL_CWD)

    def one(rec):
        c = m.call(EXPAND_TEMPLATE.format(date=rec["question_date"], question=rec["question"]), EXPAND_SYSTEM, "sonnet", "CUES", str(rec["question_id"]), "expand")
        t = c["result"]
        return {"question_id": str(rec["question_id"]), "cue_keys": [str(x) for x in json.loads(t[t.find("{"):t.rfind("}") + 1])["cue_keys"]]}
    with ThreadPoolExecutor(3) as ex:
        rows = list(ex.map(one, recs))
    with open(os.path.join(VOL, "question_cues_pilot.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("cues for", len(rows), "questions; counter", m.calls_used())


def fid(f):
    return hashlib.sha1(("%s|%s|%s|%s|%s" % (f["session_id"], f["round"], f["slot"], f["predicate"], f["object"])).encode()).hexdigest()[:16]


def stage_assemble():
    idx = load_index()
    have = cached_extractions()
    out_path = os.path.join(VOL, "facts_pilot_d500.jsonl")
    seen, nfacts, missing = set(), 0, []
    per_session = []
    with open(out_path, "w") as fh:
        for sid in sorted(idx):
            fs = have.get((sid, idx[sid]["hash"]))
            if not fs:
                missing.append(sid)
                continue
            per_session.append(len(fs))
            for f in fs:
                rec = {"fact_id": fid(f), "session_id": f["session_id"], "round": f["round"], "fact": "%s | %s | %s" % (f["subject"], f["predicate"], f["object"]),
                       "subject": f["subject"], "predicate": f["predicate"], "object": f["object"], "slot": f["slot"], "cardinality": f["cardinality"],
                       "modality": f["modality"], "author": f["author"], "scope": f["scope"], "value": f.get("value"), "unit": f.get("unit"),
                       "event_time": f.get("event_time"), "cue_keys": f["cue_keys"], "quote": f.get("quote", "")}
                if rec["fact_id"] in seen:
                    continue
                seen.add(rec["fact_id"])
                fh.write(json.dumps(rec) + "\n")
                nfacts += 1
    per_session.sort()
    n = len(per_session)
    q = lambda p: per_session[min(n - 1, int(p * n))]
    print("sessions with facts", n, "missing (failed or zero facts)", len(missing), "facts", nfacts, "facts per session mean %.2f p10 %d median %d p90 %d max %d" % (sum(per_session) / float(n), q(0.1), q(0.5), q(0.9), per_session[-1]))
    json.dump(missing, open(os.path.join(VOL, "missing_sessions.json"), "w"))


if __name__ == "__main__":
    for st in sys.argv[1:]:
        globals()["stage_" + st]()
