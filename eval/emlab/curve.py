"""Run the pooled-history curve on a question set (pilot only until the pre-registration is frozen)."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Sequence

from .corpus import cluster_ids
from .metrics import clustered_paired_bootstrap
from .pool import (LEVELS, cluster_haystacks, contamination_proxy, distribution, drop_bootstrap,
                   level_session_ids, pool_universe, shared_distractors, with_sessions)
from .retrievers import run_arm
from .runner import DEPTH, question_metrics


def run_curve(qs: Sequence, all_qs: Sequence, retriever, arms: Sequence[str], session_chars: Dict[str, int],
              n_boot: int = 2000, depth: int = DEPTH, split_name: str = "pilot") -> Dict[str, Any]:
    clusters = cluster_ids(all_qs)
    ch = cluster_haystacks(all_qs, clusters)
    universe = pool_universe(all_qs)
    sets = shared_distractors(universe, qs, split_name)
    per_q: List[Dict[str, Any]] = []
    seconds: Dict[str, List[float]] = {l: [] for l in LEVELS}
    tokens_est: Dict[str, List[float]] = {l: [] for l in LEVELS}
    contam: List[int] = []
    contam_rows: List[Dict[str, Any]] = []
    for q in qs:
        rec: Dict[str, Any] = {"qid": q.qid, "qtype": q.qtype, "levels": {}}
        for lv in LEVELS:
            ids = level_session_ids(q, universe, ch[clusters[q.qid]], lv, sets)
            pq = with_sessions(q, ids)
            tokens_est[lv].append(sum(session_chars.get(s, 0) for s in ids) / 4.0)
            t0 = time.time()
            rec["levels"][lv] = {}
            for arm in arms:
                rec["levels"][lv][arm] = question_metrics(q, run_arm(retriever, pq, arm, depth, "off"))
            seconds[lv].append(time.time() - t0)
            if lv == "H3":
                c = contamination_proxy(pq, retriever)
                if c is not None:
                    contam.append(c["above_on_both"])
                    contam_rows.append({"qid": q.qid, **c})
        per_q.append(rec)
    out: Dict[str, Any] = {"levels": {}, "tokens_estimate_chars_div_4": {}, "seconds_per_question": {},
                           "contamination_proxy": {"distribution_above_on_both": distribution(contam), "rows": contam_rows},
                           "note": "time-aware modes OFF at every level; retrieval only; gold ids unchanged"}
    for lv in LEVELS:
        out["tokens_estimate_chars_div_4"][lv] = distribution(tokens_est[lv])
        out["seconds_per_question"][lv] = distribution(seconds[lv])
        out["levels"][lv] = {}
        for arm in arms:
            vals: Dict[str, Any] = {}
            for metric in per_q[0]["levels"][lv][arm]:
                xs = [r["levels"][lv][arm][metric] for r in per_q if r["levels"][lv][arm][metric] is not None]
                vals[metric] = {"mean": sum(xs) / len(xs) if xs else float("nan"), "n": len(xs)}
                if lv != "H0":
                    vals[metric]["drop_vs_H0"] = drop_bootstrap(per_q, arm, lv, metric, clusters, n_boot)
            out["levels"][lv][arm] = vals
    out["distractor_sets"] = {k: v for k, v in sets.items() if k not in ("D500", "D5000")}
    out["per_question"] = per_q
    return out
