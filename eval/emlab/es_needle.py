"""Planted-needle control for the ES-pilot (T4.4), on OUR OWN node only (never the shared one: it needs writes).
  build  : create index emlab-rounds-v1 on the own node with the registered mapping and copy every chunk from the artifacts.
  needle : plant one needle round per pilot question (question text + a nonce; vector = the question's query vector) in its own
           session, then run the pilot's own Elasticsearch code path (es_pilot2.es_lists, level_filter) with the needle's session
           allowed (it must be rank 1 in BM25, dense and RRF) and not allowed (it must not appear in the top 200)."""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

from .build_rag_contexts import load_common
from .es_client import EsClient
from .es_pilot2 import DEPTH, LEVELS, es_lists, level_filter
from .live import ExactRetriever
from .mappings import INDEX_V1, build_mapping_v1


def build(art: str, es_url: str, batch: int = 500) -> None:
    c = EsClient(es_url, timeout=300)
    a = art.rstrip("/") + "/"
    vecs = np.load(a + "chunk_vectors.npy", mmap_mode="r")
    c.create_index(INDEX_V1, build_mapping_v1(int(vecs.shape[1]), "acorn"))
    buf: List[Dict[str, Any]] = []
    for i, line in enumerate(open(a + "chunks.jsonl", encoding="utf-8")):
        r = json.loads(line)
        v = vecs[i].tolist()
        buf.append({"doc_id": r["chunk_id"], "chunk_id": r["chunk_id"], "round_id": r["round_id"], "session_id": r["session_id"],
                    "text": r["text"], "vector": v, "vector_int8": v})
        if len(buf) >= batch:
            c.bulk(INDEX_V1, buf)
            buf = []
            if (i // batch) % 40 == 0:
                print("copied through row", i, flush=True)
    if buf:
        c.bulk(INDEX_V1, buf)
    c.refresh(INDEX_V1)
    print("count", c.request("GET", "/%s/_count" % INDEX_V1)["count"], flush=True)


def needle_id(qid: str) -> str:
    return "needle-%s" % qid


def run(dataset, art, sets_path, es_url, out_json, stamp_path):
    stamp = json.load(open(stamp_path))
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    c = EsClient(es_url, timeout=300)
    base = c.request("GET", "/%s/_count" % INDEX_V1)["count"]
    docs = []
    for q in qs:
        sid = needle_id(q.qid)
        docs.append({"doc_id": sid + ":0#0", "chunk_id": sid + ":0#0", "round_id": sid + ":0", "session_id": sid,
                     "text": "%s qzneedle%s" % (q.question, q.qid), "vector": [float(x) for x in ex.qvec[q.qid]],
                     "vector_int8": [float(x) for x in ex.qvec[q.qid]]})
    c.bulk(INDEX_V1, docs)
    c.refresh(INDEX_V1)
    total = c.request("GET", "/%s/_count" % INDEX_V1)["count"]
    all_sessions = set(ctx.hashes) | {needle_id(q.qid) for q in qs}
    rows = []
    for q in qs:
        nid = needle_id(q.qid) + ":0"
        for lv in LEVELS:
            ids = list(q.haystack_session_ids) if lv == "H0" else ctx.level_sessions(q, lv, sets)
            rec: Dict[str, Any] = {"qid": q.qid, "level": lv}
            for cond, allowed in (("present", ids + [needle_id(q.qid)]), ("absent", ids)):
                lists = es_lists(c.search, INDEX_V1, q.question, ex.qvec[q.qid], level_filter(lv, allowed, all_sessions))
                rec[cond] = {arm: (L.index(nid) + 1 if nid in L else None) for arm, L in lists.items()}
            rows.append(rec)
    summ: Dict[str, Any] = {}
    for lv in LEVELS:
        for arm in ("bm25", "dense", "rrf"):
            rs = [r for r in rows if r["level"] == lv]
            summ["%s/%s" % (lv, arm)] = {"present_rank1": sum(1 for r in rs if r["present"][arm] == 1), "present_n": len(rs),
                                         "absent_appearances": sum(1 for r in rs if r["absent"][arm] is not None),
                                         "present_ranks_not_1": sorted([r["present"][arm] for r in rs if r["present"][arm] != 1], key=lambda x: (x is None, x))[:10]}
    ok = all(v["present_rank1"] >= 93 and v["absent_appearances"] == 0 for v in summ.values())
    json.dump({"stamp": stamp, "node": "own node emlab2-eval (copy of the artifacts + needles); NOT the shared index", "docs_before_needles": base,
               "docs_after_needles": total, "needle_docs": len(docs), "summary": summ, "PASS": ok, "rows": rows}, open(out_json, "w"), indent=1)
    print(json.dumps({"PASS": ok, "docs": [base, total]}), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(*sys.argv[2:4])
    else:
        run(*sys.argv[2:8])
