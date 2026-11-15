"""Live retrievers: ExactRetriever (in-process BM25 + exact cosine over the shared chunk corpus, numpy)
and EsRetriever (Elasticsearch). Both roll chunks up to rounds by max score and honour `allowed` sessions.

ExactRetriever deviates from Elasticsearch in tokenisation (lowercase alnum, 33-word English stoplist, no
stemming) and is used for the low-overlap pre-computation and offline pilot work; an Elasticsearch
cross-check is registered as a later step.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .dataset import Question
from .es_client import EsClient
from .mappings import bm25_query, knn_query
from .retrievers import Retriever, tokens


def _rollup(pairs: Sequence[Tuple[str, float]], depth: int) -> List[Tuple[str, float]]:
    best: Dict[str, float] = {}
    for rid, s in pairs:
        if rid not in best or s > best[rid]:
            best[rid] = s
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:depth]


def _sessions(q: Question, allowed: Optional[Set[str]]) -> List[str]:
    return [s for s in q.haystack_session_ids if allowed is None or s in allowed]


class ExactRetriever(Retriever):
    """Memory-lean: keeps global document frequencies and chunk lengths only; the chunks of a question's
    haystack are tokenised on demand."""

    def __init__(self, chunks_path: str, vectors_path: str, queries_path: str, query_vectors_path: str):
        import numpy as np
        self.np = np
        self.chunks: List[Dict[str, str]] = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
        self.vectors = np.load(vectors_path, mmap_mode="r")
        qids = json.load(open(queries_path))
        self.qvec = dict(zip(qids, np.load(query_vectors_path)))
        if len(self.chunks) != len(self.vectors):
            raise ValueError("chunks and vectors differ in length")
        self.by_session: Dict[str, List[int]] = {}
        self.df: Counter = Counter()
        self.dl: List[int] = []
        for i, c in enumerate(self.chunks):
            self.by_session.setdefault(c["session_id"], []).append(i)
            toks = tokens(c["text"])
            self.dl.append(len(toks))
            self.df.update(set(toks))
        self.n = len(self.chunks)
        self.avgdl = sum(self.dl) / max(1, self.n)

    def _idx(self, q, allowed) -> List[int]:
        out: List[int] = []
        for s in _sessions(q, allowed):
            out.extend(self.by_session.get(s, ()))
        return out

    def bm25(self, q, depth, allowed):
        qt = set(tokens(q.question))
        scored = []
        for i in self._idx(q, allowed):
            tf = Counter(tokens(self.chunks[i]["text"]))
            s = 0.0
            for w in qt:
                if w in tf:
                    df = self.df[w]
                    idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                    s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * self.dl[i] / self.avgdl))
            scored.append((self.chunks[i]["round_id"], s))
        return _rollup(scored, depth)

    def dense(self, q, depth, allowed):
        idx = self._idx(q, allowed)
        if not idx:
            return []
        sims = self.np.asarray(self.vectors[idx]) @ self.qvec[q.qid]
        return _rollup([(self.chunks[i]["round_id"], float(s)) for i, s in zip(idx, sims)], depth)


class EsRetriever(Retriever):
    def __init__(self, client: EsClient, index: str, query_vectors: Dict[str, List[float]],
                 num_candidates: int = 1000, overfetch: int = 3):
        self.client, self.index, self.qv = client, index, query_vectors
        self.num_candidates, self.overfetch = num_candidates, overfetch

    def _run(self, body) -> List[Tuple[str, float]]:
        hits = self.client.search(self.index, body)["hits"]["hits"]
        return [(h["_source"]["round_id"], float(h["_score"])) for h in hits]

    def bm25(self, q, depth, allowed):
        sids = _sessions(q, allowed)
        if not sids:
            return []
        return _rollup(self._run(bm25_query(q.question, sids, min(depth * self.overfetch, 10000))), depth)

    def dense(self, q, depth, allowed):
        sids = _sessions(q, allowed)
        if not sids:
            return []
        k = min(depth * self.overfetch, 10000)
        return _rollup(self._run(knn_query(self.qv[q.qid], sids, k, max(self.num_candidates, k))), depth)
