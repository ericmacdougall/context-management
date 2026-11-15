"""Pooled-history curve (Phase R named analysis): nested haystacks of growing size, retrieval only, no LLM.

Levels (per question): H0 = own haystack; H1 = H0 + 500 distractor sessions; H2 = H0 + 5,000; H3 = the whole
pool of unique sessions. Distractors are sessions that appear in no haystack of any question in the question's
cluster; the order is a fixed seeded permutation per question (sha256 of "16096:<qid>:<session id>") so H1 is
inside H2 inside H3. Time-aware modes are OFF at pooled levels (a session has different dates in different
questions). Gold ids stay valid for retrieval metrics; distractors may contain competing answers (contamination
threat for answer-level use, measured by `contamination_proxy`).
"""
from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .dataset import SPLIT_SEED, Question
from .metrics import best_gold_rank, clustered_paired_bootstrap

LEVELS = ("H0", "H1", "H2", "H3")
D_SIZES = (("D500", 500), ("D5000", 5000))


def pool_universe(all_questions: Iterable[Question]) -> List[str]:
    seen: Dict[str, None] = {}
    for q in all_questions:
        for s in q.haystack_session_ids:
            seen.setdefault(s)
    return sorted(seen)


def shared_distractors(universe: Sequence[str], split_questions: Iterable[Question], split_name: str,
                       seed: int = SPLIT_SEED) -> Dict[str, Any]:
    """Distractor sets SHARED by every question of a split, so later answer-level arms extract facts once.

    eligible = pool sessions that appear in no haystack of any question of the split; ordered by
    sha256("<seed>:D:<split>:<session id>"); D500 is the first 500 and D5000 the first 5,000 of that order
    (D500 inside D5000). If fewer than 5,000 are eligible, D5000 is all of them and `d5000_short` is True.
    """
    used = {s for q in split_questions for s in q.haystack_session_ids}
    eligible = [s for s in universe if s not in used]
    order = sorted(eligible, key=lambda s: hashlib.sha256(("%d:D:%s:%s" % (seed, split_name, s)).encode()).hexdigest())
    return {"seed": seed, "split": split_name, "pool_sessions": len(universe), "eligible": len(eligible),
            "D500": order[:500], "D5000": order[:5000], "d5000_short": len(order) < 5000}


def level_session_ids(q: Question, universe: Sequence[str], cluster_sessions: Sequence[str], level: str,
                      sets: Dict[str, Any]) -> List[str]:
    """Session ids searched at `level`: H0 own haystack; H1 own + D500; H2 own + D5000; H3 own + every pool
    session except sessions of the question's own cluster. Own sessions come first, in their own order."""
    own = list(q.haystack_session_ids)
    if level == "H0":
        return own
    own_set = set(own)
    if level == "H1":
        extra = sets["D500"]
    elif level == "H2":
        extra = sets["D5000"]
    elif level == "H3":
        banned = set(cluster_sessions)
        extra = [s for s in universe if s not in banned]
    else:
        raise ValueError(level)
    return own + [s for s in extra if s not in own_set]


def cluster_haystacks(all_questions: Sequence[Question], clusters: Dict[str, str]) -> Dict[str, List[str]]:
    """cluster id -> every session id appearing in any haystack of any question in the cluster."""
    out: Dict[str, set] = {}
    for q in all_questions:
        out.setdefault(clusters[q.qid], set()).update(q.haystack_session_ids)
    return {c: sorted(v) for c, v in out.items()}


def with_sessions(q: Question, session_ids: Sequence[str]) -> Question:
    return dataclasses.replace(q, haystack_session_ids=list(session_ids))


def contamination_proxy(q: Question, retriever, depth: int = 10 ** 9) -> Optional[Dict[str, int]]:
    """At a pooled question (whole pool): the number of non-gold ROUNDS ranked above the best gold round on
    BOTH bm25 and dense. A non-gold round that outranks gold on both legs is a candidate competing answer."""
    gold = set(q.retrieval_gold_round_ids)
    if not gold:
        return None
    rb = [d for d, _ in retriever.bm25(q, depth, None)]
    rd = [d for d, _ in retriever.dense(q, depth, None)]
    gb, gd = best_gold_rank(rb, gold), best_gold_rank(rd, gold)
    above_b = set(rb[: (gb - 1) if gb else len(rb)]) - gold
    above_d = set(rd[: (gd - 1) if gd else len(rd)]) - gold
    return {"above_on_both": len(above_b & above_d), "above_bm25": len(above_b), "above_dense": len(above_d),
            "best_gold_rank_bm25": gb, "best_gold_rank_dense": gd, "rounds_ranked": len(rb)}


def distribution(vals: Sequence[float]) -> Dict[str, float]:
    v = sorted(vals)
    if not v:
        return {}
    q = lambda f: v[min(len(v) - 1, int(f * len(v)))]
    return {"n": len(v), "min": v[0], "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "p90": q(0.9),
            "max": v[-1], "mean": sum(v) / len(v), "n_zero": sum(1 for x in v if x == 0)}


def drop_bootstrap(per_q: List[Dict[str, Any]], arm_key: str, level: str, metric: str, clusters: Dict[str, str],
                   n_boot: int = 2000, seed: int = SPLIT_SEED) -> Optional[Dict[str, float]]:
    """Clustered paired bootstrap of (level - H0) for one arm and metric; questions without gold are skipped."""
    rows = []
    for r in per_q:
        a = r["levels"]["H0"][arm_key].get(metric)
        b = r["levels"][level][arm_key].get(metric)
        if a is None or b is None:
            continue
        rows.append((clusters[r["qid"]], a, b))
    return clustered_paired_bootstrap(rows, n_boot=n_boot, seed=seed) if rows else None
