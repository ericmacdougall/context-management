"""Runner CLI. Only --dry-run (fixture + in-memory fake retriever) is implemented in this scaffold."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from .dataset import FixtureLoader, split_pilot_test
from .metrics import best_gold_rank, is_low_overlap, ndcg_at_k, recall_at_k
from .mappings import ARMS as ARMS_ALL
from .retrievers import FakeRetriever, run_arm, to_sessions

KS = (5, 10, 20, 50)
DEPTH = 200


def evaluate(questions, retriever, depth: int = DEPTH, low_overlap_threshold: int = 100) -> Dict[str, Any]:
    per_q: List[Dict[str, Any]] = []
    for q in questions:
        rec: Dict[str, Any] = {"qid": q.qid, "qtype": q.qtype, "abstention": q.is_abstention, "arms": {}}
        for ta in (False, True):
            for arm in ARMS_ALL:
                tr = run_arm(retriever, q, arm, depth, ta)
                sr = to_sessions(tr)
                m: Dict[str, Any] = {}
                for k in KS:
                    m["session_recall@%d" % k] = recall_at_k(sr, q.gold_session_ids, k)
                    m["session_ndcg@%d" % k] = ndcg_at_k(sr, q.gold_session_ids, k)
                    m["turn_recall@%d" % k] = recall_at_k(tr, q.gold_turn_ids, k)
                    m["turn_ndcg@%d" % k] = ndcg_at_k(tr, q.gold_turn_ids, k)
                rec["arms"]["%s|time_aware=%s" % (arm, ta)] = m
        raw_b = [d for d, _ in retriever.bm25(q, depth, None)]
        raw_d = [d for d, _ in retriever.dense(q, depth, None)]
        rec["low_overlap"] = (is_low_overlap(best_gold_rank(to_sessions(raw_b), q.gold_session_ids),
                                             best_gold_rank(to_sessions(raw_d), q.gold_session_ids),
                                             low_overlap_threshold) if q.gold_session_ids else None)
        per_q.append(rec)
    summary: Dict[str, Dict[str, Dict[str, float]]] = {}
    for key in per_q[0]["arms"] if per_q else []:
        for metric in per_q[0]["arms"][key]:
            vals = [r["arms"][key][metric] for r in per_q if r["arms"][key][metric] is not None]
            summary.setdefault(key, {})[metric] = sum(vals) / len(vals) if vals else float("nan")
    return {"per_question": per_q, "mean_over_questions_with_gold": summary}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emlab.runner")
    ap.add_argument("--dry-run", action="store_true", help="fixture + in-memory fake retriever; no network")
    ap.add_argument("--dataset", default="fixture", choices=["fixture"])
    ap.add_argument("--out", help="write JSON results here")
    ap.add_argument("--es-url", help="Elasticsearch base URL (live runs; not implemented in scaffold)")
    ap.add_argument("--credential-file", help="path to a 0600 file holding the API key (live runs)")
    ap.add_argument("--phase", choices=["R", "F"], default="R")
    args = ap.parse_args(argv)
    if not args.dry_run:
        sys.stderr.write("live runs are not implemented in the scaffold: freeze PREREGISTRATION.md first\n")
        return 2
    qs = list(FixtureLoader().questions())
    res = evaluate(qs, FakeRetriever(), depth=DEPTH, low_overlap_threshold=2)  # threshold 2: fixture has 3-5 sessions
    res["split"] = split_pilot_test([(q.qid, q.qtype) for q in qs])
    res["note"] = "DRY RUN: fixture + fake retriever; not evidence about any arm"
    text = json.dumps(res, indent=1, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(json.dumps({q["qid"]: q["low_overlap"] for q in res["per_question"]}))
    for key, m in res["mean_over_questions_with_gold"].items():
        print("%-28s session_recall@5=%.3f turn_ndcg@10=%.3f" % (key, m["session_recall@5"], m["turn_ndcg@10"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
