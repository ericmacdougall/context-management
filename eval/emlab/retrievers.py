"""Retriever interface, an in-memory fake used by --dry-run, and arm composition.

The fake is NOT evidence about Elasticsearch: it only exercises the pipeline.
"""
from __future__ import annotations

import abc
import hashlib
import math
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .dataset import Question, Turn
from .fusion import linear, rrf

_TOK = re.compile(r"[a-z0-9]+")


def tokens(s: str) -> List[str]:
    return _TOK.findall(s.lower())


class Retriever(abc.ABC):
    """Returns (turn_id, score) best first, optionally restricted to turns dated <= as_of."""

    @abc.abstractmethod
    def bm25(self, q: Question, depth: int, as_of) -> List[Tuple[str, float]]: ...

    @abc.abstractmethod
    def dense(self, q: Question, depth: int, as_of) -> List[Tuple[str, float]]: ...

    @abc.abstractmethod
    def rerank(self, q: Question, ranked: List[str], depth: int) -> List[str]: ...


class FakeRetriever(Retriever):
    def _pool(self, q: Question, as_of) -> List[Turn]:
        return [t for t in q.turns if as_of is None or t.date <= as_of]

    def bm25(self, q, depth, as_of):
        pool = self._pool(q, as_of)
        docs = [tokens(t.text) for t in pool]
        n = len(docs)
        avg = sum(len(d) for d in docs) / max(1, n)
        df = Counter(w for d in docs for w in set(d))
        qt = tokens(q.question)
        out = []
        for t, d in zip(pool, docs):
            tf = Counter(d)
            s = 0.0
            for w in qt:
                if w in tf:
                    idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                    s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(d) / avg))
            out.append((t.turn_id, s))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out[:depth]

    @staticmethod
    def _vec(text: str) -> Dict[str, float]:
        s = " " + " ".join(tokens(text)) + " "
        c = Counter(s[i:i + 3] for i in range(len(s) - 2))
        norm = math.sqrt(sum(v * v for v in c.values())) or 1.0
        return {k: v / norm for k, v in c.items()}

    def dense(self, q, depth, as_of):
        qv = self._vec(q.question)
        out = []
        for t in self._pool(q, as_of):
            tv = self._vec(t.text)
            out.append((t.turn_id, sum(v * tv.get(k, 0.0) for k, v in qv.items())))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out[:depth]

    def rerank(self, q, ranked, depth):
        by = {t.turn_id: t for t in q.turns}
        qs = set(tokens(q.question))
        def key(tid):  # stand-in cross-encoder: token overlap, stable on ties
            return (-len(qs & set(tokens(by[tid].text))), ranked.index(tid))
        return sorted(ranked, key=key)[:depth]


def run_arm(r: Retriever, q: Question, arm: str, depth: int, time_aware: bool) -> List[str]:
    as_of = q.question_date if time_aware else None
    if arm == "bm25":
        return [d for d, _ in r.bm25(q, depth, as_of)]
    if arm == "dense":
        return [d for d, _ in r.dense(q, depth, as_of)]
    b, d = r.bm25(q, depth, as_of), r.dense(q, depth, as_of)
    if arm == "rrf":
        return [x for x, _ in rrf([[i for i, _ in b], [i for i, _ in d]])][:depth]
    fused = linear([dict(b), dict(d)])
    if arm == "linear":
        return [x for x, _ in fused][:depth]
    if arm == "rerank":
        base = [x for x, _ in rrf([[i for i, _ in b], [i for i, _ in d]])][:depth]
        return r.rerank(q, base, depth)
    raise ValueError("unknown arm %r" % arm)


def to_sessions(turn_ranking: Sequence[str]) -> List[str]:
    """Session ranking = sessions ordered by their best-ranked turn (turn id is 'session:index')."""
    seen: List[str] = []
    for tid in turn_ranking:
        sid = tid.rsplit(":", 1)[0]
        if sid not in seen:
            seen.append(sid)
    return seen
