"""End-metric impact of approximate dense retrieval (pilot only; new file): session recall@10 and NDCG@10 of the
hybrid (RRF 60 over exact BM25 rounds and a dense leg) at H2 and H3 when the dense leg comes from Elasticsearch
bbq_hnsw / int8_hnsw (num_candidates 100 and 1000, with and without rescore_vector oversample 3, both filter
heuristics) versus the exact dense leg. The dense leg is the top-100 chunks, rolled up to rounds by first
occurrence; BM25 is the exact in-process leg in both."""
from __future__ import annotations

import json
import sys

import numpy as np

from .corpus import cluster_ids
from .dataset import LongMemEvalLoader, select_split
from .es_client import CurlEsClient, EsError
from .escompare import set_heuristic
from .fusion import rrf
from .live import ExactRetriever
from .mappings import INDEX_V1
from .metrics import clustered_paired_bootstrap, ndcg_at_k, recall_at_k
from .pool import cluster_haystacks, level_session_ids, pool_universe, with_sessions
from .retrievers import to_sessions

K = 100


def rounds_of(chunk_ids):
    seen, out = set(), []
    for c in chunk_ids:
        r = c.split("#")[0]
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def main(dataset, art, es_url, cfg, sets_path, out):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = [q for q in select_split(all_qs, "pilot") if q.retrieval_gold_session_ids]
    sets = json.load(open(sets_path))
    cl = cluster_ids(all_qs)
    ch = cluster_haystacks(all_qs, cl)
    uni = pool_universe(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    qv = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    c = CurlEsClient(es_url, cfg, timeout=300)
    prep = {}
    for lv in ("H2", "H3"):
        for q in qs:
            ids = level_session_ids(q, uni, ch[cl[q.qid]], lv, sets)
            pq = with_sessions(q, ids)
            bm = [d for d, _ in ex.bm25(pq, K, None)]
            idx = np.asarray([i for s in ids for i in ex.by_session.get(s, ())])
            sims = ex.vectors[idx] @ qv[q.qid]
            top = np.argsort(-sims, kind="stable")[:K]
            dense_exact = rounds_of([ex.chunks[int(idx[t])]["chunk_id"] for t in top])
            excl = sorted(set(uni) - set(ids))
            flt = ({"terms": {"session_id": ids}} if lv == "H2" else
                   ({"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}))
            prep[(lv, q.qid)] = (bm, dense_exact, flt)

    def metric(q, dense):
        bm = prep[(lvl, q.qid)][0]
        hy = to_sessions([d for d, _ in rrf([bm, dense], k=60)])
        return (recall_at_k(hy, q.gold_session_ids, 10), ndcg_at_k(hy, q.gold_session_ids, 10),
                recall_at_k(to_sessions(dense), q.gold_session_ids, 10))

    rows, status = [], {}
    base = {}
    for lvl in ("H2", "H3"):
        base[lvl] = {q.qid: metric(q, prep[(lvl, q.qid)][1]) for q in qs}
        n = len(qs)
        rows.append({"level": lvl, "dense_leg": "exact", "hybrid_sess_recall10": sum(v[0] for v in base[lvl].values()) / n,
                     "hybrid_sess_ndcg10": sum(v[1] for v in base[lvl].values()) / n,
                     "dense_only_sess_recall10": sum(v[2] for v in base[lvl].values()) / n, "n": n})
    for heur in ("acorn", "fanout"):
        status[heur] = set_heuristic(c, INDEX_V1, heur)
        for lvl in ("H2", "H3"):
            for field in ("vector", "vector_int8"):
                for nc in (100, 1000):
                    for resc in ("none", "oversample3"):
                        got = {}
                        err = None
                        for q in qs:
                            knn = {"field": field, "query_vector": [float(x) for x in qv[q.qid]], "k": K,
                                   "num_candidates": nc, "filter": prep[(lvl, q.qid)][2]}
                            if resc == "oversample3":
                                knn["rescore_vector"] = {"oversample": 3}
                            else:
                                knn["rescore_vector"] = {"oversample": 0}
                            try:
                                resp = c.search(INDEX_V1, {"size": K, "_source": False, "knn": knn})
                            except EsError as e:
                                err = str(e)[:200]
                                break
                            got[q.qid] = rounds_of([h["_id"] for h in resp["hits"]["hits"]])
                        row = {"level": lvl, "heuristic": heur, "field": field, "num_candidates": nc, "rescore": resc}
                        if err:
                            row["error"] = err
                        else:
                            res = {q.qid: metric(q, got[q.qid]) for q in qs}
                            n = len(qs)
                            row.update({"hybrid_sess_recall10": sum(v[0] for v in res.values()) / n,
                                        "hybrid_sess_ndcg10": sum(v[1] for v in res.values()) / n,
                                        "dense_only_sess_recall10": sum(v[2] for v in res.values()) / n, "n": n})
                            d = clustered_paired_bootstrap([(cl[q.qid], base[lvl][q.qid][0], res[q.qid][0]) for q in qs],
                                                           n_boot=2000, seed=16096)
                            row["hybrid_recall10_diff_vs_exact"] = {k: d[k] for k in ("mean_diff", "ci_low", "ci_high", "effective_n")}
                        rows.append(row)
                        print(row, flush=True)
    json.dump({"split": "pilot", "heuristic_status": status, "rows": rows}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:7])
