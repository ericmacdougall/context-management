"""Stdlib helpers shared by the exploratory experiments (tested locally)."""
from __future__ import annotations

import random
from typing import Any, Dict, List

from .metrics import wilson_interval


def derangement_list(n: int, seed: int) -> List[int]:
    """A seeded permutation of range(n) with no fixed point (every item carries another item's content)."""
    rng = random.Random(seed)
    p = list(range(n))
    while True:
        rng.shuffle(p)
        if all(p[i] != i for i in range(n)):
            return p


def cluster_wilson(per_q: Dict[str, float], clusters: Dict[str, str]) -> Dict[str, Any]:
    """Wilson 95% interval where the unit is the content cluster: the cluster rate is the mean over its questions and the
    Wilson formula is applied to the mean of cluster rates with n = number of clusters (real-valued successes)."""
    by: Dict[str, List[float]] = {}
    for q, v in per_q.items():
        by.setdefault(clusters[q], []).append(v)
    cm = [sum(v) / len(v) for v in by.values()]
    n = len(cm)
    p = sum(cm) / n
    lo, hi = wilson_interval(p * n, n)
    return {"n_questions": len(per_q), "n_clusters": n, "rate": p, "wilson_low": lo, "wilson_high": hi}
