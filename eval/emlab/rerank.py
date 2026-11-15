"""Cross-encoder reranker (BAAI/bge-reranker-v2-m3 on CPU). Imports torch lazily; not used by tests."""
from __future__ import annotations

from typing import Dict, List

from .retrievers import Retriever


class RerankingWrapper(Retriever):
    """Scores (question, chunk) pairs and takes the max over a round's chunks (same chunk rule as the dense
    side, so long rounds are not silently truncated). Scores are cached by (qid, round_id) because rerank
    runs under every time-aware mode on mostly the same candidates."""

    def __init__(self, base: Retriever, round_chunks: Dict[str, List[str]], model_dir: str, threads: int = 12,
                 max_length: int = 512, batch: int = 8):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(threads)
        name = "BAAI/bge-reranker-v2-m3"
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(name, cache_dir=model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=model_dir).eval()
        self.base, self.chunks, self.max_length, self.batch = base, round_chunks, max_length, batch
        self.cache: Dict[tuple, float] = {}

    def bm25(self, q, depth, allowed):
        return self.base.bm25(q, depth, allowed)

    def dense(self, q, depth, allowed):
        return self.base.dense(q, depth, allowed)

    def _score_pairs(self, query: str, texts: List[str]) -> List[float]:
        out: List[float] = []
        for i in range(0, len(texts), self.batch):
            enc = self.tok([query] * len(texts[i:i + self.batch]), texts[i:i + self.batch], padding=True,
                           truncation="only_second", max_length=self.max_length, return_tensors="pt")
            with self.torch.inference_mode():
                out.extend(self.model(**enc).logits.view(-1).tolist())
        return out

    def rerank(self, q, ranked: List[str], depth: int) -> List[str]:
        todo = [r for r in ranked if (q.qid, r) not in self.cache]
        pairs = [(r, t) for r in todo for t in self.chunks[r]]
        scores = self._score_pairs(q.question, [t for _, t in pairs])
        best: Dict[str, float] = {}
        for (r, _), s in zip(pairs, scores):
            best[r] = max(best.get(r, float("-inf")), s)
        for r, s in best.items():
            self.cache[(q.qid, r)] = s
        order = sorted(range(len(ranked)), key=lambda j: (-self.cache[(q.qid, ranked[j])], j))
        return [ranked[j] for j in order][:depth]
