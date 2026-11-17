"""Retriever interface, an in-memory fake for --dry-run, and arm/mode composition.

Time-aware modes reduce to a set of allowed session ids (computed client-side from the question's own
haystack dates) so Elasticsearch needs only a `terms` filter on session_id:
  off            every session in the question's haystack
  asof           sessions whose calendar day <= question calendar day (day granularity)
  expand_filter  asof, intersected with the date range parsed from the question (falls back to asof
                 when no range is parsed or the intersection is empty)
  expand_boost   asof, then sessions inside the parsed range get a score multiplier (1 + beta)
The fake retriever is NOT evidence about Elasticsearch: it only exercises the pipeline.
"""
from __future__ import annotations

import abc
import math
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .dataset import Question, Round, visible_as_of
from .fusion import linear, rrf
from .timeparse import in_range, parse_question_range

MODES = ("off", "asof", "expand_filter", "expand_boost")
BOOST_BETA = 0.5  # default; pilot grid {0.25, 0.5, 1.0}
RERANK_TOP_N = 20  # the ONE registered rerank head size (dated pilot-stage amendment 2026-11-15; was 50)

_STOP = frozenset("a an and are as at be but by for if in into is it no not of on or such that the their "
                  "then there these they this to was will with".split())
_TOK = re.compile(r"[a-z0-9]+")


def tokens(s: str) -> List[str]:
    return [w for w in _TOK.findall(s.lower()) if w not in _STOP]


def session_of(round_id: str) -> str:
    return round_id.rsplit(":", 1)[0]


def session_dates(q: Question) -> Dict[str, "object"]:
    out = {}
    for t in q.turns:
        out.setdefault(t.session_id, t.date)
    return out


def allowed_sessions(q: Question, mode: str) -> Tuple[Optional[Set[str]], Set[str]]:
    """Returns (allowed session ids or None for 'no restriction', boost-session ids)."""
    if mode == "off":
        return None, set()
    dates = session_dates(q)
    asof = {s for s, d in dates.items() if visible_as_of(d, q.question_date)}
    if mode == "asof":
        return asof, set()
    rng = parse_question_range(q.question, q.question_date)
    inside = {s for s in asof if rng and in_range(dates[s].date(), rng)}
    if mode == "expand_filter":
        return (inside if inside else asof), set()
    if mode == "expand_boost":
        return asof, inside
    raise ValueError("unknown mode %r" % mode)


def apply_boost(ranked: Sequence[str], boost_sessions: Set[str], beta: float = BOOST_BETA,
                k: int = 60) -> List[str]:
    if not boost_sessions:
        return list(ranked)
    scored = []
    for i, rid in enumerate(ranked):
        s = 1.0 / (k + i + 1)
        if session_of(rid) in boost_sessions:
            s *= 1.0 + beta
        scored.append((-s, i, rid))
    return [rid for _, _, rid in sorted(scored)]


class Retriever(abc.ABC):
    """bm25/dense return (round_id, score) best first, restricted to `allowed` sessions when not None."""

    @abc.abstractmethod
    def bm25(self, q: Question, depth: int, allowed: Optional[Set[str]]) -> List[Tuple[str, float]]: ...

    @abc.abstractmethod
    def dense(self, q: Question, depth: int, allowed: Optional[Set[str]]) -> List[Tuple[str, float]]: ...

    def rerank(self, q: Question, ranked: List[str], depth: int) -> List[str]:
        raise NotImplementedError


class FakeRetriever(Retriever):
    def _pool(self, q: Question, allowed) -> List[Round]:
        return [r for r in q.rounds if allowed is None or r.session_id in allowed]

    def bm25(self, q, depth, allowed):
        pool = self._pool(q, allowed)
        docs = [tokens(r.text) for r in pool]
        n = len(docs)
        avg = sum(len(d) for d in docs) / max(1, n)
        df = Counter(w for d in docs for w in set(d))
        qt = tokens(q.question)
        out = []
        for r, d in zip(pool, docs):
            tf = Counter(d)
            s = 0.0
            for w in qt:
                if w in tf:
                    idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                    s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(d) / avg))
            out.append((r.round_id, s))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out[:depth]

    @staticmethod
    def _vec(text: str) -> Dict[str, float]:
        s = " " + " ".join(tokens(text)) + " "
        c = Counter(s[i:i + 3] for i in range(len(s) - 2))
        norm = math.sqrt(sum(v * v for v in c.values())) or 1.0
        return {k: v / norm for k, v in c.items()}

    def dense(self, q, depth, allowed):
        qv = self._vec(q.question)
        out = []
        for r in self._pool(q, allowed):
            tv = self._vec(r.text)
            out.append((r.round_id, sum(v * tv.get(k, 0.0) for k, v in qv.items())))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out[:depth]

    def rerank(self, q, ranked, depth):
        by = {r.round_id: r for r in q.rounds}
        qs = set(tokens(q.question))
        def key(rid):  # stand-in cross-encoder: token overlap, stable on ties
            return (-len(qs & set(tokens(by[rid].text))), ranked.index(rid))
        return sorted(ranked, key=key)[:depth]


def run_arm(r: Retriever, q: Question, arm: str, depth: int, mode: str,
            rrf_k: int = 60, dense_weight: float = 0.5, beta: float = BOOST_BETA,
            rerank_top: int = RERANK_TOP_N) -> List[str]:
    """Round ranking for one arm under one time-aware mode. Boost (expand_boost) is applied to the fused
    list before reranking; rerank reorders only the top `rerank_top`, the tail keeps its fused order."""
    allowed, boost = allowed_sessions(q, mode)
    if arm == "bm25":
        out = [d for d, _ in r.bm25(q, depth, allowed)]
    elif arm == "dense":
        out = [d for d, _ in r.dense(q, depth, allowed)]
    elif arm in ("linear", "rrf", "rerank"):
        b, d = r.bm25(q, depth, allowed), r.dense(q, depth, allowed)
        if arm == "linear":
            out = [x for x, _ in linear([dict(b), dict(d)], [1.0 - dense_weight, dense_weight])][:depth]
        else:
            out = [x for x, _ in rrf([[i for i, _ in b], [i for i, _ in d]], k=rrf_k)][:depth]
    else:
        raise ValueError("unknown arm %r" % arm)
    out = apply_boost(out, boost, beta)
    if arm == "rerank":
        head = out[:rerank_top]
        out = r.rerank(q, head, len(head)) + out[rerank_top:]
    return out[:depth]


def to_sessions(round_ranking: Sequence[str]) -> List[str]:
    """Session ranking = sessions ordered by their best-ranked round."""
    seen: List[str] = []
    s = set()
    for rid in round_ranking:
        sid = session_of(rid)
        if sid not in s:
            s.add(sid)
            seen.append(sid)
    return seen
