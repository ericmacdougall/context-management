"""Pooled-history curve (Phase R named analysis): nested haystacks of growing size, retrieval only, no LLM.

Amended after the independent review (content identity, matched levels). The pool is DEDUPLICATED BY CONTENT: one
canonical session id per content hash (the smallest id). Per question:
  H0 = own haystack
  H1 = own + D500
  H2 = own + DL            (L = the largest round number both the pilot and the test split can supply)
  H3 = own + every canonical pool session whose content is in no haystack of the question's content cluster
D sets are SHARED by all questions of a split. Eligible distractors: canonical sessions whose content appears in NO
haystack of the split's questions and is gold (by content) for NO question in any split; ordered by
sha256("<seed>:D:<split>:<session id>"); D500 inside DL. Because a content that is in the split's haystacks is
excluded, no distractor is a twin of any own-haystack session. Time-aware modes are off at pooled levels.
"""
from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .content import content_clusters, gold_round_hashes, round_hashes, session_hashes
from .dataset import SPLIT_SEED, Question
from .metrics import best_gold_rank, clustered_paired_bootstrap

LEVELS = ("H0", "H1", "H2", "H3")


class PoolContext:
    def __init__(self, all_questions: Sequence[Question]):
        self.qs = list(all_questions)
        self.hashes = session_hashes(self.qs)                      # session id -> content hash
        by: Dict[str, List[str]] = {}
        for s, h in self.hashes.items():
            by.setdefault(h, []).append(s)
        self.canon = {h: min(v) for h, v in by.items()}            # content hash -> canonical session id
        self.pool = sorted(self.canon.values())                    # deduplicated pool (canonical ids)
        self.gold_hashes = {self.hashes[s] for q in self.qs for s in q.gold_session_ids if s in self.hashes}
        self.clusters = content_clusters(self.qs, self.hashes)     # qid -> content cluster id
        self.round_h = round_hashes(self.qs)                       # session id -> hashes of its rounds
        self.gold_round_h = {q.qid: gold_round_hashes(q) for q in self.qs}
        self.hay_hashes = {q.qid: {self.hashes[s] for s in q.haystack_session_ids} for q in self.qs}
        self.cluster_hay: Dict[str, set] = {}
        for q in self.qs:
            self.cluster_hay.setdefault(self.clusters[q.qid], set()).update(self.hay_hashes[q.qid])

    def eligible(self, split_questions: Iterable[Question]) -> List[str]:
        used = set()
        for q in split_questions:
            used |= self.hay_hashes[q.qid]
        banned = used | self.gold_hashes
        return [self.canon[h] for h in self.canon if h not in banned]

    def shared_distractors(self, split_questions: Sequence[Question], split_name: str, L: int,
                           seed: int = SPLIT_SEED) -> Dict[str, Any]:
        el = self.eligible(split_questions)
        order = sorted(el, key=lambda s: hashlib.sha256(("%d:D:%s:%s" % (seed, split_name, s)).encode()).hexdigest())
        return {"seed": seed, "split": split_name, "pool_sessions_deduplicated": len(self.pool),
                "session_ids_total": len(self.hashes), "eligible": len(el), "L": L,
                "D500": order[:500], "DL": order[:L], "short": len(order) < L}

    def matched_L(self, pilot_qs: Sequence[Question], test_qs: Sequence[Question], step: int = 100) -> Dict[str, int]:
        ep, et = len(self.eligible(pilot_qs)), len(self.eligible(test_qs))
        return {"eligible_pilot": ep, "eligible_test": et, "L": (min(ep, et) // step) * step}

    def level_sessions(self, q: Question, level: str, sets: Dict[str, Any]) -> List[str]:
        own = list(q.haystack_session_ids)
        if level == "H0":
            return own
        if level == "H1":
            extra = sets["D500"]
        elif level == "H2":
            extra = sets["DL"]
        elif level == "H3":
            banned = self.cluster_hay[self.clusters[q.qid]]
            extra = [s for s in self.pool if self.hashes[s] not in banned]
        else:
            raise ValueError(level)
        own_h = {self.hashes[s] for s in own}
        own_set = set(own)
        gr = self.gold_round_h[q.qid]
        # residual rule (review M1): drop a distractor that shares a ROUND-level content hash with a gold round of this
        # question, even if the rest of the session differs
        out = own + [s for s in extra if s not in own_set and self.hashes[s] not in own_h
                     and not (self.round_h.get(s, set()) & gr)]
        # assertion: gold matching by content holds; no extra session is a twin of an own session or holds a gold round
        for s in out[len(own):]:
            assert self.hashes[s] not in own_h and not (self.round_h.get(s, set()) & gr), "content twin in level"
        return out


def with_sessions(q: Question, session_ids: Sequence[str]) -> Question:
    return dataclasses.replace(q, haystack_session_ids=list(session_ids))


def contamination_proxy(q: Question, retriever, depth: int = 10 ** 9) -> Optional[Dict[str, int]]:
    """At the whole-pool level: the number of non-gold ROUNDS ranked above the best gold round on BOTH bm25 and dense.
    Content twins of gold are absent from the level by construction (amendment A)."""
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
    rows = []
    for r in per_q:
        a = r["levels"]["H0"][arm_key].get(metric)
        b = r["levels"][level][arm_key].get(metric)
        if a is None or b is None:
            continue
        rows.append((clusters[r["qid"]], a, b))
    return clustered_paired_bootstrap(rows, n_boot=n_boot, seed=seed) if rows else None
