"""Round-level parity (pilot, H0): the reported pipeline (exact BM25 and exact dense ROUND lists fused client-side by
RRF 60 / min-max linear) vs Elasticsearch's own rrf and linear retrievers (chunk hits rolled up to rounds by first
occurrence), and ES BM25 vs the exact BM25. Differences are reported, nothing is tuned. New file."""
from __future__ import annotations

import json
import sys

import numpy as np

from .dataset import LongMemEvalLoader, select_split
from .es_client import CurlEsClient
from .escompare import overlap, set_heuristic
from .fusion import linear, rrf
from .live import ExactRetriever
from .mappings import INDEX_V1, bm25_query, knn_query
from .pool import with_sessions


def rounds_of(chunk_ids):
    seen, out = set(), []
    for c in chunk_ids:
        r = c.split("#")[0]
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def main(dataset, art, es_url, cfg, out):
    qs = select_split(list(LongMemEvalLoader(dataset).questions()), "pilot")
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    qv = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    c = CurlEsClient(es_url, cfg, timeout=300)
    status = set_heuristic(c, INDEX_V1, "acorn")
    rows = []
    for q in qs:
        sess = q.haystack_session_ids
        eb = [d for d, _ in ex.bm25(q, 200, None)]
        ed = [d for d, _ in ex.dense(q, 200, None)]
        mine_rrf = [d for d, _ in rrf([eb, ed], k=60)]
        sc_b, sc_d = dict(ex.bm25(q, 200, None)), dict(ex.dense(q, 200, None))
        mine_lin = [d for d, _ in linear([sc_b, sc_d], [0.5, 0.5])]
        b = bm25_query(q.question, sess, 200)
        k = knn_query([float(x) for x in qv[q.qid]], sess, 200, 1000)
        std, knn = {"standard": {"query": b["query"]}}, {"knn": k["knn"]}
        es_b = rounds_of([h["_id"] for h in c.search(INDEX_V1, b)["hits"]["hits"]])
        es_rrf = rounds_of([h["_id"] for h in c.search(INDEX_V1, {"size": 200, "_source": False, "retriever": {"rrf": {
            "retrievers": [std, knn], "rank_window_size": 200, "rank_constant": 60}}})["hits"]["hits"]])
        es_lin = rounds_of([h["_id"] for h in c.search(INDEX_V1, {"size": 200, "_source": False, "retriever": {"linear": {
            "retrievers": [{"retriever": std, "weight": 0.5, "normalizer": "minmax"},
                           {"retriever": knn, "weight": 0.5, "normalizer": "minmax"}], "rank_window_size": 200}}})["hits"]["hits"]])
        rows.append({"qid": q.qid, "bm25_es_vs_exact_top10": overlap(es_b, eb, 10),
                     "rrf_top10": overlap(mine_rrf, es_rrf, 10), "rrf_same_order10": mine_rrf[:10] == es_rrf[:10],
                     "linear_top10": overlap(mine_lin, es_lin, 10), "linear_same_order10": mine_lin[:10] == es_lin[:10]})
    n = len(rows)
    summ = {k: sum(float(r[k]) for r in rows) / n for k in rows[0] if k != "qid"}
    json.dump({"split": "pilot", "level": "H0", "heuristic_status": status, "n": n, "summary": summ, "rows": rows},
              open(out, "w"), indent=1)
    print(summ)


if __name__ == "__main__":
    main(*sys.argv[1:6])
