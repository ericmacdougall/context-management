"""Bulk-index chunks + vectors into Elasticsearch. Do NOT run until the node owner hands the node over."""
from __future__ import annotations

import argparse
import json

from .es_client import EsClient
from .mappings import build_mapping


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--credential-file")
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--index", default="emlab-chunks")
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args(argv)
    import numpy as np
    d = a.artifacts.rstrip("/") + "/"
    vecs = np.load(d + "chunk_vectors.npy")
    c = EsClient(a.es_url, a.credential_file, timeout=120)
    c.create_index(a.index, build_mapping(int(vecs.shape[1])))
    buf = []
    for i, line in enumerate(open(d + "chunks.jsonl", encoding="utf-8")):
        r = json.loads(line)
        buf.append({"doc_id": r["chunk_id"], "chunk_id": r["chunk_id"], "round_id": r["round_id"],
                    "session_id": r["session_id"], "text": r["text"],
                    "vector": vecs[i].tolist()})
        if len(buf) >= a.batch:
            c.bulk(a.index, buf)
            buf = []
    if buf:
        c.bulk(a.index, buf)
    c.refresh(a.index)
    print("indexed", len(vecs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
