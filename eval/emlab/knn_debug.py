"""Diagnose worst-case kNN recall at H3 (new file): are the true top-10 chunks tied or near-duplicates?"""
import json, sys
import numpy as np
from .corpus import cluster_ids
from .dataset import LongMemEvalLoader, select_split
from .es_client import CurlEsClient
from .live import ExactRetriever
from .mappings import INDEX_V1
from .pool import cluster_haystacks, level_session_ids, pool_universe

def main(dataset, art, es_url, cfg, sets_path):
    all_qs = list(LongMemEvalLoader(dataset).questions()); qs = select_split(all_qs, "pilot")
    sets = json.load(open(sets_path)); cl = cluster_ids(all_qs); ch = cluster_haystacks(all_qs, cl); uni = pool_universe(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    qv = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    c = CurlEsClient(es_url, cfg, timeout=300)
    c.request("PUT", "/%s/_settings" % INDEX_V1, {"index.dense_vector.hnsw_filter_heuristic": "fanout"})
    res = []
    for q in qs:
        ids = level_session_ids(q, uni, ch[cl[q.qid]], "H3", sets)
        idx = np.asarray([i for s in ids for i in ex.by_session.get(s, ())])
        sims = ex.vectors[idx] @ qv[q.qid]
        top = np.argsort(-sims, kind="stable")[:10]
        truth = [ex.chunks[int(idx[t])]["chunk_id"] for t in top]
        excl = sorted(set(uni) - set(ids))
        flt = {"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}
        body = {"size": 10, "_source": False, "knn": {"field": "vector", "query_vector": [float(x) for x in qv[q.qid]], "k": 10, "num_candidates": 1000, "filter": flt}}
        got = [h["_id"] for h in c.search(INDEX_V1, body)["hits"]["hits"]]
        ov = len(set(got) & set(truth)) / 10.0
        res.append((ov, q.qid, q.question[:60], float(sims[top[0]]), float(sims[top[9]]), len(got), len(excl)))
    res.sort()
    for r in res[:6]:
        print(r)
    print("n with overlap<0.5:", sum(1 for r in res if r[0] < 0.5), "of", len(res), "; n got<10:", sum(1 for r in res if r[5] < 10))

if __name__ == "__main__":
    main(*sys.argv[1:6])
