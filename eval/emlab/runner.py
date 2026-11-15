"""Runner CLI for Phase R (retrieval only, no LLM).

  --dry-run                     fixture + in-memory fake retriever, no network, no files needed
  --retriever exact|es          live: real dataset, precomputed vectors (embed.py), exact in-process or ES
  --action run|lowoverlap|grid|index
  --split pilot|test|all        test results must not be produced before the pre-registration is frozen
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import Any, Dict, List, Optional

from .dataset import FixtureLoader, LongMemEvalLoader, Question, select_split, split_pilot_test
from .mappings import ARMS
from .metrics import best_gold_rank, is_low_overlap, ndcg_at_k, recall_at_k
from .retrievers import MODES, FakeRetriever, Retriever, run_arm, to_sessions

KS = (5, 10, 20, 50)
DEPTH = 200
GRID_RRF_K = (20, 60, 100)
GRID_DENSE_W = (0.3, 0.5, 0.7)
GRID_BETA = (0.25, 0.5, 1.0)


def low_overlap(q: Question, retriever: Retriever, depth: int, threshold: int) -> Optional[bool]:
    gold = q.retrieval_gold_session_ids
    if not gold:
        return None  # abstention / no gold: never low-overlap
    rb = best_gold_rank(to_sessions([d for d, _ in retriever.bm25(q, depth, None)]), gold)
    rd = best_gold_rank(to_sessions([d for d, _ in retriever.dense(q, depth, None)]), gold)
    return is_low_overlap(rb, rd, threshold)


def question_metrics(q: Question, rounds: List[str]) -> Dict[str, Optional[float]]:
    sr = to_sessions(rounds)
    gs, gr = q.retrieval_gold_session_ids, q.retrieval_gold_round_ids
    m: Dict[str, Optional[float]] = {}
    for k in KS:
        m["session_recall@%d" % k] = recall_at_k(sr, gs, k)
        m["session_ndcg@%d" % k] = ndcg_at_k(sr, gs, k)
        m["round_recall@%d" % k] = recall_at_k(rounds, gr, k)
        m["round_ndcg@%d" % k] = ndcg_at_k(rounds, gr, k)
        # knowledge-update only: the CURRENT-value session must be retrieved; stale-only is a miss
        m["gold_latest_hit@%d" % k] = recall_at_k(sr, q.gold_latest_session_ids, k)
    return m


def evaluate(questions, retriever: Retriever, depth: int = DEPTH, low_overlap_threshold: int = 100,
             arms=ARMS, modes=MODES, **cfg) -> Dict[str, Any]:
    per_q: List[Dict[str, Any]] = []
    for q in questions:
        rec: Dict[str, Any] = {"qid": q.qid, "qtype": q.qtype, "abstention": q.is_abstention,
                               "late_session_subset": bool(q.late_session_ids), "arms": {}}
        for mode in modes:
            for arm in arms:
                rec["arms"]["%s|%s" % (arm, mode)] = question_metrics(q, run_arm(retriever, q, arm, depth, mode, **cfg))
        rec["low_overlap"] = low_overlap(q, retriever, depth, low_overlap_threshold)
        per_q.append(rec)
    return {"per_question": per_q, "summary": summarise(per_q)}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else float("nan"), len(vals)


def summarise(per_q: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mean over questions WITH retrieval gold, overall and per question type and per overlay; n alongside."""
    groups = {"all": lambda r: True}
    for t in sorted({r["qtype"] for r in per_q}):
        groups["type=" + t] = (lambda t: lambda r: r["qtype"] == t)(t)
    groups["low_overlap"] = lambda r: r["low_overlap"] is True
    groups["late_session_subset"] = lambda r: r["late_session_subset"]
    out: Dict[str, Any] = {}
    for g, f in groups.items():
        rows = [r for r in per_q if f(r)]
        if not rows:
            continue
        gd: Dict[str, Any] = {}
        for key in rows[0]["arms"]:
            gd[key] = {}
            for metric in rows[0]["arms"][key]:
                mean, n = _mean(r["arms"][key][metric] for r in rows)
                gd[key][metric] = {"mean": mean, "n": n}
        out[g] = gd
    return out


def grid(questions, retriever: Retriever, depth: int = DEPTH) -> List[Dict[str, Any]]:
    """Pilot tuning: mean session NDCG@10 over questions with gold, per configuration. Selection rule in the
    pre-registration: the configuration with the highest mean, ties to the smaller value of each parameter."""
    rows = []
    for k in GRID_RRF_K:
        res = evaluate(questions, retriever, depth, arms=("rrf",), modes=("asof",), rrf_k=k)
        rows.append({"arm": "rrf", "rrf_k": k, "session_ndcg@10": res["summary"]["all"]["rrf|asof"]["session_ndcg@10"]})
    for w in GRID_DENSE_W:
        res = evaluate(questions, retriever, depth, arms=("linear",), modes=("asof",), dense_weight=w)
        rows.append({"arm": "linear", "dense_weight": w, "session_ndcg@10": res["summary"]["all"]["linear|asof"]["session_ndcg@10"]})
    for b in GRID_BETA:
        res = evaluate(questions, retriever, depth, arms=("rrf",), modes=("expand_boost",), beta=b)
        rows.append({"arm": "rrf+boost", "beta": b, "session_ndcg@10": res["summary"]["all"]["rrf|expand_boost"]["session_ndcg@10"]})
    return rows


def _write(res, out):
    text = json.dumps(res, indent=1, default=str)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emlab.runner")
    ap.add_argument("--dry-run", action="store_true", help="fixture + in-memory fake retriever; no network")
    ap.add_argument("--phase", choices=["R", "F"], default="R")
    ap.add_argument("--action", choices=["run", "lowoverlap", "grid", "index"], default="run")
    ap.add_argument("--retriever", choices=["exact", "es"], default="exact")
    ap.add_argument("--dataset-path")
    ap.add_argument("--artifacts", help="dir written by embed.py (chunks.jsonl, vectors, queries)")
    ap.add_argument("--split", choices=["pilot", "test", "all"], default="pilot")
    ap.add_argument("--seed", type=int, default=16096, help="split seed (must equal the registered seed)")
    ap.add_argument("--es-url")
    ap.add_argument("--credential-file", help="path to a 0600 file holding the API key")
    ap.add_argument("--index", default="emlab-chunks")
    ap.add_argument("--reranker-dir", help="model cache dir; enables the rerank arm")
    ap.add_argument("--low-overlap-threshold", type=int, default=100)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    if args.dry_run:
        qs = list(FixtureLoader().questions())
        res = evaluate(qs, FakeRetriever(), depth=DEPTH, low_overlap_threshold=2, arms=[a for a in ARMS])
        res["split"] = split_pilot_test([(q.qid, q.qtype) for q in qs])
        res["note"] = "DRY RUN: fixture + fake retriever; not evidence about any arm"
        _write(res, args.out)
        print(json.dumps({q["qid"]: q["low_overlap"] for q in res["per_question"]}))
        for key, m in res["summary"]["all"].items():
            print("%-28s session_recall@5=%.3f round_ndcg@10=%.3f" % (key, m["session_recall@5"]["mean"], m["round_ndcg@10"]["mean"]))
        return 0

    if args.phase != "R":
        sys.stderr.write("Phase F is not implemented in this scaffold\n")
        return 2
    if args.seed != 16096:
        sys.stderr.write("split seed must be the registered 16096\n")
        return 2
    if not args.dataset_path or not args.artifacts:
        sys.stderr.write("--dataset-path and --artifacts are required for live runs\n")
        return 2
    if args.split != "pilot" and args.action != "index":
        sys.stderr.write("NOTE: %s split requested; only valid after the pre-registration hash is recorded\n" % args.split)
    qs = select_split(list(LongMemEvalLoader(args.dataset_path).questions()), args.split)
    a = args.artifacts.rstrip("/") + "/"
    if args.retriever == "exact":
        from .live import ExactRetriever
        retr: Retriever = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    else:
        from .es_client import EsClient
        from .live import EsRetriever
        import numpy as np
        client = EsClient(args.es_url, args.credential_file)
        qids = json.load(open(a + "queries.json"))
        qv = {k: v.tolist() for k, v in zip(qids, np.load(a + "query_vectors.npy"))}
        retr = EsRetriever(client, args.index, qv)
    arms = list(ARMS)
    if args.reranker_dir:
        from .rerank import RerankingWrapper
        rt = {}
        for line in open(a + "rounds.jsonl", encoding="utf-8"):
            r = json.loads(line)
            rt[r["round_id"]] = r["text"]
        retr = RerankingWrapper(retr, rt, args.reranker_dir)
    else:
        arms.remove("rerank")
    if args.action == "lowoverlap":
        res = {"split": args.split, "threshold": args.low_overlap_threshold,
               "low_overlap": {q.qid: low_overlap(q, retr, DEPTH, args.low_overlap_threshold) for q in qs}}
    elif args.action == "grid":
        res = {"split": args.split, "grid": grid(qs, retr)}
    elif args.action == "run":
        res = evaluate(qs, retr, low_overlap_threshold=args.low_overlap_threshold, arms=arms)
        res["split"] = args.split
    else:
        sys.stderr.write("index: use emlab.indexer after the node owner hands the container over\n")
        return 2
    _write(res, args.out)
    print("wrote", args.out, "questions", len(qs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
