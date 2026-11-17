"""Whole-pool low-overlap overlay (the REGISTERED definition, Section 3.2): best gold ROUND rank over the full ranking
of the deduplicated pool minus the question's content cluster (level H3), raw question, no time filter, BM25 and
dense; low-overlap iff worse than the threshold on BOTH. Generator for ANY split (the runner refuses non-pilot splits
until the freeze hash is recorded). Abstention questions and questions without gold are never low-overlap."""
from __future__ import annotations

from typing import Any, Dict, Sequence

from .metrics import best_gold_rank, is_low_overlap
from .pool import PoolContext, with_sessions

FULL = 10 ** 9
THRESHOLDS = (100, 50, 20)


def compute(qs: Sequence, ctx: PoolContext, retriever) -> Dict[str, Any]:
    ranks: Dict[str, Any] = {}
    for q in qs:
        gold = q.retrieval_gold_round_ids
        if not gold:
            continue
        pq = with_sessions(q, ctx.level_sessions(q, "H3", {}))
        rb = best_gold_rank([d for d, _ in retriever.bm25(pq, FULL, None)], gold)
        rd = best_gold_rank([d for d, _ in retriever.dense(pq, FULL, None)], gold)
        ranks[q.qid] = {"bm25": rb, "dense": rd}
    out: Dict[str, Any] = {"generator": "emlab.lowoverlap_pool.compute (runner --action lowoverlap_pool)",
                           "unit": "best gold round rank over the whole deduplicated pool minus the content cluster (H3)",
                           "n_with_gold": len(ranks), "ranks": ranks, "thresholds": {}}
    for t in THRESHOLDS:
        sel = [qid for qid, r in ranks.items() if is_low_overlap(r["bm25"], r["dense"], t)]
        out["thresholds"][str(t)] = {"n": len(sel), "qids": sorted(sel)}
    return out
