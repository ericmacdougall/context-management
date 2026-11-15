"""kNN comparison at pooled filters H2 and H3 (pilot only; new file): ES filtered kNN vs exact cosine, both vector
fields, both heuristics, num_candidates 100 and 1000; recall of true top-10 / top-50 chunks and latency."""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from .dataset import LongMemEvalLoader, select_split
from .es_client import CurlEsClient
from .escompare import exact_topk, overlap, set_heuristic
from .live import ExactRetriever
from .mappings import INDEX_V1
from .pool import PoolContext


def main(dataset, art, es_url, cfg, sets_path, out, limit=0):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(all_qs, "pilot")
    if int(limit):
        qs = qs[:int(limit)]
    sets = json.load(open(sets_path))
    ctx = PoolContext(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    qv = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    c = CurlEsClient(es_url, cfg, timeout=300)
    rows = []
    status = {}
    truth = {}
    sess = {}
    for lv in ("H2", "H3"):
        for q in qs:
            ids = ctx.level_sessions(q, lv, sets)
            sess[(lv, q.qid)] = ids
            truth[(lv, q.qid)] = exact_topk(ex.vectors, ex.by_session, ex.chunks, qv[q.qid], ids, 50)
    for heur in ("acorn", "fanout"):
        status[heur] = set_heuristic(c, INDEX_V1, heur)
        for lv in ("H2", "H3"):
            for field in ("vector", "vector_int8"):
                for nc in (100, 1000):
                    r10, r50, took, wall = [], [], [], []
                    for q in qs:
                        ids = sess[(lv, q.qid)]
                        if lv == "H3":  # same set expressed as a must_not on the excluded own-cluster sessions
                            excl = sorted(set(ctx.hashes) - set(ids))
                            flt = {"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}
                        else:
                            flt = {"terms": {"session_id": ids}}
                        body = {"size": 50, "_source": False, "knn": {"field": field, "query_vector": [float(x) for x in qv[q.qid]],
                                                                       "k": 50, "num_candidates": nc, "filter": flt}}
                        t0 = time.time()
                        resp = c.search(INDEX_V1, body)
                        wall.append(time.time() - t0)
                        took.append(resp.get("took", 0))
                        got = [h["_id"] for h in resp["hits"]["hits"]]
                        tr = truth[(lv, q.qid)]
                        r10.append(overlap(got, tr, 10))
                        r50.append(len(set(got) & set(tr)) / 50.0)
                    n = len(qs)
                    rows.append({"level": lv, "heuristic": heur, "field": field, "num_candidates": nc,
                                 "recall_true_top10_mean": sum(r10) / n, "min_top10": min(r10),
                                 "recall_true_top50_mean": sum(r50) / n,
                                 "es_took_ms_mean": sum(took) / n, "wall_s_mean": sum(wall) / n,
                                 "wall_s_max": max(wall), "n_questions": n})
                    print(rows[-1], flush=True)
    json.dump({"split": "pilot", "heuristic_setting_status": status, "rows": rows}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:8])
