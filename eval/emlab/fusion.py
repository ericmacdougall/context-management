"""Client-side fusion, so runs never depend on a licensed Elasticsearch fusion feature."""
from __future__ import annotations

from typing import Dict, Hashable, List, Optional, Sequence, Tuple


def rrf(rankings: Sequence[Sequence[Hashable]], k: int = 60,
        weights: Optional[Sequence[float]] = None) -> List[Tuple[Hashable, float]]:
    """Reciprocal rank fusion: score(d) = sum_i w_i / (k + rank_i(d)), rank 1-based.

    Ties are broken by first appearance across the input lists, then list order, so output is deterministic.
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights/rankings length mismatch")
    score: Dict[Hashable, float] = {}
    first: Dict[Hashable, Tuple[int, int]] = {}
    for li, (lst, w) in enumerate(zip(rankings, weights)):
        seen = set()
        for pos, d in enumerate(lst):
            if d in seen:
                continue
            seen.add(d)
            score[d] = score.get(d, 0.0) + w / (k + pos + 1)
            first.setdefault(d, (pos, li))
    return sorted(score.items(), key=lambda kv: (-kv[1], first[kv[0]]))


def _minmax(scores: Dict[Hashable, float]) -> Dict[Hashable, float]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        return {d: 1.0 for d in scores}
    return {d: (s - lo) / (hi - lo) for d, s in scores.items()}


def linear(score_lists: Sequence[Dict[Hashable, float]],
           weights: Optional[Sequence[float]] = None) -> List[Tuple[Hashable, float]]:
    """Weighted sum of per-list min-max normalised scores; a doc missing from a list contributes 0.

    Ties are broken by id repr for determinism.
    """
    if weights is None:
        weights = [1.0] * len(score_lists)
    if len(weights) != len(score_lists):
        raise ValueError("weights/score_lists length mismatch")
    total: Dict[Hashable, float] = {}
    for sl, w in zip(score_lists, weights):
        for d, s in _minmax(sl).items():
            total[d] = total.get(d, 0.0) + w * s
    return sorted(total.items(), key=lambda kv: (-kv[1], repr(kv[0])))
