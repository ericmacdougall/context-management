"""Index mapping and query bodies for the Phase R arms (Elasticsearch 9.5.x; syntax to re-check on the node).

One shared index of unique chunks (see corpus.py); every query is filtered to the question's own haystack
session ids (and, for time-aware modes, to the allowed subset computed client-side). Vectors are computed
outside Elasticsearch with one embedding model for every arm (labelled deviation from semantic_text).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

ARMS = ("bm25", "dense", "rrf", "linear", "rerank")


def build_mapping(dims: Optional[int], similarity: str = "cosine", index_type: str = "bbq_hnsw") -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "chunk_id": {"type": "keyword"},
        "round_id": {"type": "keyword"},
        "session_id": {"type": "keyword"},
        "text": {"type": "text", "analyzer": "english"},
    }
    if dims is not None:  # BM25-only arm needs no vector field
        props["vector"] = {"type": "dense_vector", "dims": dims, "index": True, "similarity": similarity,
                           "index_options": {"type": index_type}}
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": props}}


def _filters(session_ids: Sequence[str]) -> List[Dict[str, Any]]:
    return [{"terms": {"session_id": list(session_ids)}}]


def bm25_query(text: str, session_ids: Sequence[str], size: int) -> Dict[str, Any]:
    return {"size": size, "_source": ["round_id", "session_id"],
            "query": {"bool": {"must": [{"match": {"text": text}}], "filter": _filters(session_ids)}}}


def knn_query(vector: List[float], session_ids: Sequence[str], size: int,
              num_candidates: int = 1000) -> Dict[str, Any]:
    return {"size": size, "_source": ["round_id", "session_id"],
            "knn": {"field": "vector", "query_vector": vector, "k": size,
                    "num_candidates": num_candidates, "filter": _filters(session_ids)}}
