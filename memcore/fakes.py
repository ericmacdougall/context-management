"""Deterministic fakes for tests: a scripted ChatModel, hash-based embedder, overlap reranker, in-memory search."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Callable, Dict, List, Optional, Sequence

from .chat_base import BackendOut, CachedChatModel
from .fusion import linear, rrf
from .ports import Capabilities, Embedder, Hit, Reranker, RerankHit, SearchBackend, SearchResult, Usage

_TOK = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> List[str]:
    return _TOK.findall(text.lower())


class FakeChat(CachedChatModel):
    """Backend is a python callable (prompt, system, schema) -> str | dict; counts real calls in `.backend_calls`."""
    adapter_id = "fake"
    network_safe = True          # positively declared: a stub, never a subscription backend

    def __init__(self, fn: Callable, reports_usage: bool = True, **kw):
        super().__init__(model="fake-1", **kw)
        self.fn, self.backend_calls = fn, 0
        self.capabilities = Capabilities(adapter="fake", supports_json_schema=False, json_schema_mode="prompt+validate",
                                         supports_system=True, reports_usage=reports_usage, max_context_tokens=1000,
                                         supports_temperature=True, supports_max_output_tokens=True)

    def _call_backend(self, prompt, system, json_schema, timeout_s, params=None):
        self.backend_calls += 1
        self.last_params = params
        r = self.fn(prompt, system, json_schema)
        text = r if isinstance(r, str) else json.dumps(r)
        usage = Usage(len(prompt) // 4, 0, len(text) // 4) if self.capabilities.reports_usage else None
        return BackendOut(text=text, usage=usage)


class HashEmbedder(Embedder):
    """Bag-of-words hashed into `dimensions` buckets, L2-normalised. E5-style prefixes are applied by the port."""
    model = "fake-hash-embed"
    query_prefix = "query: "
    passage_prefix = "passage: "

    def __init__(self, dimensions: int = 64):
        self.dimensions = dimensions

    def _embed(self, texts, kind):
        out = []
        for t in texts:
            v = [0.0] * self.dimensions
            for w in tokens(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dimensions] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class OverlapReranker(Reranker):
    model = "fake-overlap-rerank"

    def rerank(self, query, documents, top_n=None):
        q = set(tokens(query))
        scored = [RerankHit(i, len(q & set(tokens(d))) / (len(q) or 1)) for i, d in enumerate(documents)]
        scored.sort(key=lambda h: (-h.score, h.index))
        return scored[:top_n] if top_n else scored


class InMemorySearch(SearchBackend):
    """Indices of {id: {"text": str, "vector": [...], **fields}}; filter is exact-match on fields."""
    fusion_location = "client"

    def __init__(self):
        self.indices: Dict[str, Dict[str, dict]] = {}

    def add(self, index: str, doc_id: str, doc: dict) -> None:
        self.indices.setdefault(index, {})[doc_id] = doc

    def _docs(self, index, filter):
        for i, d in self.indices.get(index, {}).items():
            if all(d.get(k) == v for k, v in (filter or {}).items()):
                yield i, d

    def lexical(self, index, query, filter=None, k=10):
        q = set(tokens(query))
        sc = [(i, len(q & set(tokens(d["text"]))) / (len(q) or 1)) for i, d in self._docs(index, filter)]
        sc = sorted([s for s in sc if s[1] > 0], key=lambda s: (-s[1], s[0]))[:k]
        return SearchResult([Hit(i, s) for i, s in sc], "lexical", "none")

    def vector(self, index, vector, filter=None, k=10):
        sc = [(i, sum(a * b for a, b in zip(vector, d["vector"]))) for i, d in self._docs(index, filter) if "vector" in d]
        sc = sorted(sc, key=lambda s: (-s[1], s[0]))[:k]
        return SearchResult([Hit(i, s) for i, s in sc], "vector", "none")

    def fused(self, index, query, vector, filter=None, k=10):
        lex, vec = self.lexical(index, query, filter, 100), self.vector(index, vector, filter, 100)
        fused = rrf([[h.id for h in lex.hits], [h.id for h in vec.hits]])[:k]
        return SearchResult([Hit(i, s) for i, s in fused], "fused", "client")
