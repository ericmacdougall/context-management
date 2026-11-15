"""Metric functions. All ranks are 1-based; rankings are lists of ids, best first."""
from __future__ import annotations

import math
import random
from typing import Dict, Hashable, List, Optional, Sequence, Tuple

Z95 = 1.959963984540054


def recall_at_k(ranked: Sequence[Hashable], gold: Sequence[Hashable], k: int) -> Optional[float]:
    """Fraction of distinct gold ids present in the top k. None when there is no gold (abstention)."""
    gold_set = set(gold)
    if not gold_set:
        return None
    if k < 1:
        raise ValueError("k must be >= 1")
    top = set(ranked[:k])
    return len(top & gold_set) / len(gold_set)


def ndcg_at_k(ranked: Sequence[Hashable], gold: Sequence[Hashable], k: int) -> Optional[float]:
    """Binary-gain NDCG@k with log2(rank+1) discount. Duplicate ids in `ranked` earn gain once."""
    gold_set = set(gold)
    if not gold_set:
        return None
    if k < 1:
        raise ValueError("k must be >= 1")
    seen = set()
    dcg = 0.0
    for i, item in enumerate(ranked[:k]):
        if item in gold_set and item not in seen:
            seen.add(item)
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold_set), k)))
    return dcg / ideal


def best_gold_rank(ranked: Sequence[Hashable], gold: Sequence[Hashable]) -> Optional[int]:
    """1-based rank of the best-ranked gold id, None if no gold id appears in `ranked`."""
    gold_set = set(gold)
    for i, item in enumerate(ranked):
        if item in gold_set:
            return i + 1
    return None


def is_low_overlap(bm25_rank: Optional[int], dense_rank: Optional[int], threshold: int = 100) -> bool:
    """Low-overlap stratum: best gold rank is worse than `threshold` (or absent) on BOTH BM25 and dense."""
    def worse(r: Optional[int]) -> bool:
        return r is None or r > threshold
    return worse(bm25_rank) and worse(dense_rank)


def wilson_interval(successes: int, n: int, z: float = Z95) -> Tuple[float, float]:
    """Wilson score interval for a single proportion. Never use on a difference of proportions."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def clustered_paired_bootstrap(
    rows: Sequence[Tuple[Hashable, float, float]],
    n_boot: int = 10000,
    seed: int = 0,
    alpha: float = 0.05,
) -> Dict[str, float]:
    """Paired bootstrap of mean(b - a), resampling whole clusters (conversations) with replacement.

    rows: (cluster_id, value_arm_a, value_arm_b), one per question. Returns the observed mean
    difference, a percentile interval, and the effective n (number of clusters).
    """
    clusters: Dict[Hashable, List[float]] = {}
    for cid, a, b in rows:
        clusters.setdefault(cid, []).append(b - a)
    keys = sorted(clusters, key=repr)
    if not keys:
        raise ValueError("no rows")
    sums = [sum(clusters[c]) for c in keys]
    cnts = [len(clusters[c]) for c in keys]
    total_n = sum(cnts)
    observed = sum(sums) / total_n
    rng = random.Random(seed)
    m = len(keys)
    diffs = []
    for _ in range(n_boot):
        s = 0.0
        c = 0
        for _ in range(m):
            j = rng.randrange(m)
            s += sums[j]
            c += cnts[j]
        diffs.append(s / c)
    diffs.sort()
    lo = diffs[int((alpha / 2) * n_boot)]
    hi = diffs[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return {"mean_diff": observed, "ci_low": lo, "ci_high": hi, "effective_n": float(m),
            "questions": float(total_n), "n_boot": float(n_boot), "seed": float(seed)}
