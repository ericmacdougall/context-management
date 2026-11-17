"""Create and fill the second index `emlab-facts-v1` (facts + cue keys + vector). Modest bulk. Run only after the
facts file and facts_vectors.npy exist. New in phase 3."""
from __future__ import annotations

import argparse
import json
import os
import time

from .es_client import CurlEsClient
from .facts import load_facts

INDEX_FACTS = "emlab-facts-v1"


def mapping(dims: int = 384):
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"properties": {
                "fact_id": {"type": "keyword"}, "session_id": {"type": "keyword"}, "round_index": {"type": "integer"},
                "text": {"type": "text", "analyzer": "english"}, "cue_keys": {"type": "text", "analyzer": "english"},
                "slot": {"type": "keyword"}, "modality": {"type": "keyword"}, "event_time": {"type": "keyword"},
                "vector": {"type": "dense_vector", "dims": dims, "index": True, "similarity": "cosine",
                           "index_options": {"type": "bbq_hnsw"}}}}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--curl-config", required=True)
    ap.add_argument("--facts", required=True)
    ap.add_argument("--batch", type=int, default=200)
    a = ap.parse_args(argv)
    import numpy as np
    facts = load_facts(a.facts)
    vecs = np.load(os.path.join(os.path.dirname(a.facts), "facts_vectors.npy"))
    c = CurlEsClient(a.es_url, a.curl_config)
    c.create_index(INDEX_FACTS, mapping(int(vecs.shape[1])))
    buf = []
    for f, v in zip(facts, vecs):
        buf.append({"doc_id": f["id"], "fact_id": f["id"], "session_id": f["session_id"], "round_index": int(f["round_index"]),
                    "text": f["text"], "cue_keys": " ".join(f["cue_keys"]), "slot": f.get("slot"),
                    "modality": f.get("modality"), "event_time": str(f.get("event_time") or ""), "vector": v.tolist()})
        if len(buf) >= a.batch:
            c.bulk(INDEX_FACTS, buf)
            buf = []
            time.sleep(0.3)
    if buf:
        c.bulk(INDEX_FACTS, buf)
    c.refresh(INDEX_FACTS)
    print("indexed", len(facts))


if __name__ == "__main__":
    main()
