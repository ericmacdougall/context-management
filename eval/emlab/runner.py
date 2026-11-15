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
from .retrievers import FakeRetriever, Retriever, run_arm, to_sessions

KS = (5, 10, 20, 50)
DEPTH = 200
# `asof` is identical to `off` on LongMemEval-S (0 sessions removed), so it is not run as a separate arm.
RUN_MODES = ("off", "expand_filter", "expand_boost")
GRID_RRF_K = (20, 60, 100)
GRID_DENSE_W = (0.3, 0.5, 0.7)
GRID_BETA = (0.25, 0.5, 1.0)


FULL = 10 ** 9  # no depth cut: the full haystack is ranked


def low_overlap(q: Question, retriever: Retriever, depth: int, threshold: int) -> Optional[bool]:
    """Low-overlap on the best gold ROUND rank over the FULL haystack ranking (no depth cut), raw question,
    no time filter; true iff that rank is > threshold on BOTH bm25 and dense. `depth` is ignored (kept for
    call compatibility). Abstention / no-gold questions are never low-overlap."""
    gold = q.retrieval_gold_round_ids
    if not gold:
        return None
    rb = best_gold_rank([d for d, _ in retriever.bm25(q, FULL, None)], gold)
    rd = best_gold_rank([d for d, _ in retriever.dense(q, FULL, None)], gold)
    return is_low_overlap(rb, rd, threshold)


def low_overlap_ranks(q: Question, retriever: Retriever) -> Optional[Dict[str, Optional[int]]]:
    gold = q.retrieval_gold_round_ids
    if not gold:
        return None
    return {"bm25": best_gold_rank([d for d, _ in retriever.bm25(q, FULL, None)], gold),
            "dense": best_gold_rank([d for d, _ in retriever.dense(q, FULL, None)], gold)}


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
             arms=ARMS, modes=RUN_MODES, **cfg) -> Dict[str, Any]:
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
        res = evaluate(questions, retriever, depth, arms=("rrf",), modes=("off",), rrf_k=k)
        rows.append({"arm": "rrf", "rrf_k": k, "session_ndcg@10": res["summary"]["all"]["rrf|off"]["session_ndcg@10"]})
    for w in GRID_DENSE_W:
        res = evaluate(questions, retriever, depth, arms=("linear",), modes=("off",), dense_weight=w)
        rows.append({"arm": "linear", "dense_weight": w, "session_ndcg@10": res["summary"]["all"]["linear|off"]["session_ndcg@10"]})
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
    ap.add_argument("--action", choices=["run", "lowoverlap", "grid", "index", "diagnostics", "rerank-timing", "curve", "poolsets"],
                    default="run")
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
    if args.split != "pilot" and args.action not in ("index", "diagnostics"):
        sys.stderr.write("NOTE: %s split requested; only valid after the pre-registration hash is recorded\n" % args.split)
    qs = select_split(list(LongMemEvalLoader(args.dataset_path).questions()), args.split)
    a = args.artifacts.rstrip("/") + "/"
    if args.action == "poolsets":  # ids only, for other owners to extract facts once
        if args.split != "pilot":
            sys.stderr.write("poolsets: only the pilot sets are built now; the test-split rule is registered, not built\n")
            return 2
        from .pool import pool_universe, shared_distractors
        all_qs = list(LongMemEvalLoader(args.dataset_path).questions())
        res = shared_distractors(pool_universe(all_qs), qs, "pilot")
        _write(res, args.out)
        print("D500", len(res["D500"]), "D5000", len(res["D5000"]), "eligible", res["eligible"])
        return 0
    if args.action == "diagnostics":  # structural only, reads no retrieval result
        from .diagnostics import asof_report, parser_report
        res = {"split": args.split, "asof": asof_report(qs), "parser": parser_report(qs)}
        _write(res, args.out)
        print(json.dumps(res))
        return 0
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
        rt: Dict[str, List[str]] = {}
        for line in open(a + "chunks.jsonl", encoding="utf-8"):
            r = json.loads(line)
            rt.setdefault(r["round_id"], []).append(r["text"])
        retr = RerankingWrapper(retr, rt, args.reranker_dir)
    else:
        arms.remove("rerank")
    if args.action == "curve":
        from .curve import run_curve
        all_qs = list(LongMemEvalLoader(args.dataset_path).questions())
        chars: Dict[str, int] = {}
        for c in retr.chunks:  # ExactRetriever only
            chars[c["session_id"]] = chars.get(c["session_id"], 0) + len(c["text"])
        retr.prepare_terms(qs)
        res = run_curve(qs, all_qs, retr, arms, chars)
        res["split"] = args.split
    elif args.action == "rerank-timing":
        import time
        from .retrievers import allowed_sessions, rrf as _rrf
        rows = []
        for q in [x for x in qs if x.retrieval_gold_round_ids][:5]:
            allowed, _ = allowed_sessions(q, "asof")
            b, d = retr.bm25(q, DEPTH, allowed), retr.dense(q, DEPTH, allowed)
            head = [x for x, _ in _rrf([[i for i, _ in b], [i for i, _ in d]])][:50]
            t0 = time.time()
            retr.rerank(q, head, 50)
            rows.append({"qid": q.qid, "seconds_for_top50": time.time() - t0})
        res = {"rows": rows, "mean_seconds": sum(r["seconds_for_top50"] for r in rows) / max(1, len(rows))}
    elif args.action == "lowoverlap":
        ranks = {q.qid: low_overlap_ranks(q, retr) for q in qs}
        flags = {q.qid: (None if ranks[q.qid] is None else is_low_overlap(ranks[q.qid]["bm25"], ranks[q.qid]["dense"], args.low_overlap_threshold)) for q in qs}
        res = {"split": args.split, "threshold": args.low_overlap_threshold, "unit": "gold round, full haystack ranking",
               "ranks": ranks, "low_overlap": flags,
               "n_with_gold": sum(v is not None for v in flags.values()), "n_low_overlap": sum(v is True for v in flags.values())}
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
