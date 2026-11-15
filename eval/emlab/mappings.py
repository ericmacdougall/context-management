"""Index mappings and query bodies for the Phase R arms (Elasticsearch 9.5.x; syntax to re-check on the node).

One index per embedding model; documents are turns, filtered per question by `qid`. Vectors are computed
outside Elasticsearch (labelled deviation from the semantic_text path) and are identical in every arm.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ARMS = ("bm25", "dense", "rrf", "linear", "rerank")


def build_mapping(dims: Optional[int], similarity: str = "cosine") -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "qid": {"type": "keyword"},
        "session_id": {"type": "keyword"},
        "turn_id": {"type": "keyword"},
        "role": {"type": "keyword"},
        "session_date": {"type": "date", "format": "strict_date_hour_minute"},
        "text": {"type": "text", "analyzer": "english"},
    }
    if dims is not None:  # BM25-only arm needs no vector field
        props["vector"] = {"type": "dense_vector", "dims": dims, "index": True, "similarity": similarity}
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": props}}


def _filters(qid: str, as_of: Optional[str]) -> List[Dict[str, Any]]:
    f: List[Dict[str, Any]] = [{"term": {"qid": qid}}]
    if as_of:  # time-aware arm: nothing after the question date may be retrieved
        f.append({"range": {"session_date": {"lte": as_of}}})
    return f


def bm25_query(text: str, qid: str, size: int, as_of: Optional[str] = None) -> Dict[str, Any]:
    return {"size": size, "_source": ["session_id", "turn_id"],
            "query": {"bool": {"must": [{"match": {"text": text}}], "filter": _filters(qid, as_of)}}}


def knn_query(vector: List[float], qid: str, size: int, as_of: Optional[str] = None,
              num_candidates: int = 1000) -> Dict[str, Any]:
    return {"size": size, "_source": ["session_id", "turn_id"],
            "knn": {"field": "vector", "query_vector": vector, "k": size,
                    "num_candidates": num_candidates, "filter": _filters(qid, as_of)}}
