"""Client-side fusion (so nothing depends on a licensed engine feature). Deterministic tie-breaks."""
from __future__ import annotations

from typing import Dict, Hashable, List, Optional, Sequence, Tuple


def rrf(rankings: Sequence[Sequence[Hashable]], k: int = 60, weights: Optional[Sequence[float]] = None) -> List[Tuple[Hashable, float]]:
    """score(d) = sum_i w_i / (k + rank_i(d)), rank 1-based; duplicates inside one list count once."""
    weights = list(weights) if weights is not None else [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights and rankings differ in length")
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
    return sorted(score.items(), key=lambda kv: (-kv[1], first[kv[0]], repr(kv[0])))


def linear(score_lists: Sequence[Dict[Hashable, float]], weights: Optional[Sequence[float]] = None) -> List[Tuple[Hashable, float]]:
    """Weighted sum of per-list min-max normalised scores; a doc absent from a list contributes 0."""
    weights = list(weights) if weights is not None else [1.0] * len(score_lists)
    if len(weights) != len(score_lists):
        raise ValueError("weights and score_lists differ in length")
    total: Dict[Hashable, float] = {}
    for sl, w in zip(score_lists, weights):
        if not sl:
            continue
        lo, hi = min(sl.values()), max(sl.values())
        for d, s in sl.items():
            total[d] = total.get(d, 0.0) + w * (1.0 if hi == lo else (s - lo) / (hi - lo))
    return sorted(total.items(), key=lambda kv: (-kv[1], repr(kv[0])))
