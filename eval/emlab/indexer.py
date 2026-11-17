"""Bulk-index chunks + vectors into the one registered index (emlab-rounds-v1). Modest bulk size with a pause
so other agents on the node are not starved. Resumable with --start (chunk row offset)."""
from __future__ import annotations

import argparse
import json
import time

from .es_client import CurlEsClient
from .mappings import INDEX_V1, build_mapping_v1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--curl-config", required=True, help="path to a curl config file (never read or printed here)")
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--index", default=INDEX_V1)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--heuristic", choices=["acorn", "fanout"])
    a = ap.parse_args(argv)
    import numpy as np
    d = a.artifacts.rstrip("/") + "/"
    vecs = np.load(d + "chunk_vectors.npy", mmap_mode="r")
    c = CurlEsClient(a.es_url, a.curl_config)
    if a.create:
        c.create_index(a.index, build_mapping_v1(int(vecs.shape[1]), a.heuristic))
    buf = []
    n = 0
    for i, line in enumerate(open(d + "chunks.jsonl", encoding="utf-8")):
        if i < a.start:
            continue
        r = json.loads(line)
        v = vecs[i].tolist()
        buf.append({"doc_id": r["chunk_id"], "chunk_id": r["chunk_id"], "round_id": r["round_id"],
                    "session_id": r["session_id"], "text": r["text"], "vector": v, "vector_int8": v})
        if len(buf) >= a.batch:
            c.bulk(a.index, buf)
            n += len(buf)
            buf = []
            time.sleep(a.pause)
            if (i // a.batch) % 50 == 0:
                print("indexed through row", i, flush=True)
    if buf:
        c.bulk(a.index, buf)
    c.refresh(a.index)
    print("done; indexed rows from", a.start, "to", len(vecs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
