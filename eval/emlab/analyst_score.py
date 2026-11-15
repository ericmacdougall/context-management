"""Score analyst arms from returned ids and the call log (no LLM). Compares, per level, the analyst arms with
RRF (the tier-0 candidates) and, when a rankings file exists, the cross-encoder rerank arm, all packed to the same
budget with the same rule; overall and on the pool-level low-overlap questions. Stale-only counts as a miss.
Model-token cost comes from the other owner's ledger, not from here."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .context import context_metrics, pack_rag

BUDGET = 4000


def precision_proxy(q, items) -> Any:
    rounds = [i["round_id"] for i in items]
    if not rounds:
        return None
    gold = set(q.retrieval_gold_round_ids)
    return sum(1 for r in rounds if r not in gold) / len(rounds)  # share of returned items that are non-gold


def score_one(q, ordered_ids: List[str], dates: Dict[str, str], texts: Dict[str, str], budget: int = BUDGET) -> Dict[str, Any]:
    ids = [r for r in ordered_ids if r.rsplit(":", 1)[0] in dates and r in texts][:20]
    items = pack_rag(ids, dates, texts, budget)
    m = context_metrics(q, items)
    m["nongold_share"] = precision_proxy(q, items)
    return m


def call_stats(log_path: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            r = json.loads(line)
            k = "%s|%s" % (r["level"], r["qid"])
            s = out.setdefault(k, {"calls": 0, "tool_seconds": 0.0})
            s["calls"] += 1
            s["tool_seconds"] += float(r.get("seconds", 0))
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def aggregate(rows: List[Dict[str, Any]], low: set) -> Dict[str, Any]:
    def agg(sel):
        ku = [r for r in sel if r["gold_latest_in_context"] is not None]
        return {"n": len(sel), "gold_session_recall": mean(r["gold_session_recall"] for r in sel),
                "gold_round_recall": mean(r["gold_round_recall"] for r in sel),
                "nongold_share": mean(r["nongold_share"] for r in sel),
                "tokens_used": mean(r["tokens_used"] for r in sel),
                "ku_n": len(ku), "ku_gold_latest_hit": mean(1.0 if r["gold_latest_in_context"] else 0.0 for r in ku),
                "ku_stale_only": mean(1.0 if r["stale_only"] else 0.0 for r in ku),
                "tool_calls": mean(r.get("calls") for r in sel), "tool_seconds": mean(r.get("tool_seconds") for r in sel)}
    return {"all": agg(rows), "low_overlap": agg([r for r in rows if r["qid"] in low])}
