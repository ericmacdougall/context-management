"""Dry run of the analyst runner against the FAKE backend on 2 pilot questions (own-haystack, lexical top-50 as stand-in candidates).
Usage: python3 -m phasef.analyst_dryrun <cap>"""
import json
import os
import sys

from . import data, run_pilot as R
from .analyst import make_llm, run_analyst, run_rerank
from .analyst_fake import FakeBackend

OUT = os.path.join(R.OUT, "analyst_dryrun")
QIDS = ["0862e8bf", "6e984301"]


def build_tasks():
    S, _ = R.pilot()
    os.makedirs(OUT, exist_ok=True)
    paths = []
    for q in QIDS:
        rounds = []
        for sid, d, turns in data.sessions_of(S[q]):
            for r in data.rounds_of(sid, turns, d):
                rounds.append({"round_id": r.round_id, "session_id": sid, "date": d, "text": r.text})
        p = os.path.join(OUT, q + ".fake.json")
        json.dump({"haystack": rounds}, open(p, "w"))
        be = FakeBackend(p)
        top = be({"tool": "search", "args": {"query": S[q]["question"], "k": 50}})["results"]
        by = {r["round_id"]: r for r in rounds}
        cands = [by[x["round_id"]] for x in top]
        json.dump({"question_id": q, "question": S[q]["question"], "question_date": S[q]["question_date"], "level": "own_haystack", "candidates": cands, "haystack": rounds}, open(p, "w"))
        paths.append(p)
    return paths


def main(cap):
    paths = build_tasks()
    ledger = os.path.join(OUT, "ledger.jsonl")
    cache = os.path.join(OUT, "cache")
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    mr = make_llm(cache, ledger, int(cap), R.NEUTRAL_CWD)
    for p in paths:
        task = json.load(open(p))
        r1 = run_rerank(task, mr)
        r2 = run_analyst(task, os.path.join(OUT, "run"), ["--fake", p], cache, ledger, int(cap), R.NEUTRAL_CWD)
        for r in (r1, r2):
            print(json.dumps({k: v for k, v in r.items() if k not in ("ranked_round_ids", "reasons", "tool_log")}), "top3", (r.get("ranked_round_ids") or [])[:3])
        print("  analyst tools:", [(x["tool"], x["args"]) for x in r2.get("tool_log", [])])
        json.dump([r1, r2], open(os.path.join(OUT, task["question_id"] + ".results.json"), "w"))


if __name__ == "__main__":
    main(sys.argv[1])
