"""Cross-encoder reranker (BAAI/bge-reranker-v2-m3 on CPU). Imports torch lazily; not used by tests."""
from __future__ import annotations

from typing import Dict, List

from .retrievers import Retriever


class RerankingWrapper(Retriever):
    def __init__(self, base: Retriever, round_text: Dict[str, str], model_dir: str, threads: int = 12,
                 max_length: int = 512, batch: int = 8):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(threads)
        name = "BAAI/bge-reranker-v2-m3"
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(name, cache_dir=model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=model_dir).eval()
        self.base, self.text, self.max_length, self.batch = base, round_text, max_length, batch

    def bm25(self, q, depth, allowed):
        return self.base.bm25(q, depth, allowed)

    def dense(self, q, depth, allowed):
        return self.base.dense(q, depth, allowed)

    def rerank(self, q, ranked: List[str], depth: int) -> List[str]:
        scores: List[float] = []
        for i in range(0, len(ranked), self.batch):
            ids = ranked[i:i + self.batch]
            enc = self.tok([q.question] * len(ids), [self.text[r] for r in ids], padding=True, truncation=True,
                           max_length=self.max_length, return_tensors="pt")
            with self.torch.inference_mode():
                scores.extend(self.model(**enc).logits.view(-1).tolist())
        order = sorted(range(len(ranked)), key=lambda j: (-scores[j], j))
        return [ranked[j] for j in order][:depth]
