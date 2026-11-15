import sys, statistics, concurrent.futures as cf
import numpy as np
from esc import *
phase = sys.argv[1]; N = 200000; DIMS = 384; H = os.path.expanduser("~/emlab-16096")
PFX = os.environ.get("E6PFX", "e6"); MODE = os.environ.get("E6MODE", "random"); RESCORE = float(os.environ.get("E6RESCORE", "0"))
def vecs(seed, n):
    r = np.random.default_rng(seed)
    if MODE == "clustered":   # 2000 gaussian clusters on the sphere; doc = centroid + noise (norm ~1)
        C = np.random.default_rng(4242).standard_normal((2000, DIMS), dtype=np.float32); C /= np.linalg.norm(C, axis=1, keepdims=True)
        v = C[r.integers(0, 2000, n)] + r.standard_normal((n, DIMS), dtype=np.float32) / np.float32(DIMS ** 0.5)
    else:
        v = r.standard_normal((n, DIMS), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True); return v
def grp(n): return np.random.default_rng(99).integers(0, 1000, n)
def cause(j):
    e = j.get("error", {}); 
    return {"type": e.get("type"), "reason": e.get("reason"), "caused_by": (e.get("caused_by") or {}).get("reason"), "root": (e.get("root_cause") or [{}])[0].get("reason")} if isinstance(e, dict) else j
CFG = {f"{PFX}-bbq-acorn": ("bbq_hnsw", "acorn"), f"{PFX}-bbq-fanout": ("bbq_hnsw", "fanout"), f"{PFX}-int8-acorn": ("int8_hnsw", "acorn"), f"{PFX}-int8-fanout": ("int8_hnsw", "fanout")}
if phase == "ingest":
    out = {"synthetic": MODE + " unit vectors (seeded numpy standard normal, normalised), 384 dims, 200k docs, grp uniform int 0..999"}
    for ix, (typ, heur) in CFG.items():
        req("DELETE", f"/{ix}")
        c, j, _ = req("PUT", f"/{ix}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1", "index.dense_vector.hnsw_filter_heuristic": heur},
              "mappings": {"properties": {"grp": {"type": "integer"}, "v": {"type": "dense_vector", "dims": DIMS, "similarity": "cosine", "index_options": {"type": typ}}}}})
        out[ix + "_create"] = (c, j if c >= 300 else "ok")
        out[ix + "_heuristic_setting"] = req("GET", f"/{ix}/_settings", filter_path="**.hnsw_filter_heuristic")[1]
    t0 = time.time(); V = vecs(1234, N); G = grp(N); np.save(f"{H}/{PFX}_vecs.npy", V); np.save(f"{H}/{PFX}_grp.npy", G)
    B = 2000
    def send(ix, s):
        nd = "".join('{"index":{"_id":"%d"}}\n%s\n' % (n, json.dumps({"grp": int(G[n]), "v": V[n].tolist()})) for n in range(s, min(N, s + B)))
        c, j, _ = req("POST", f"/{ix}/_bulk", ndjson=nd); 
        if j.get("errors"): raise RuntimeError(str(j["items"][0])[:500])
    for s in range(0, N, B):
        with cf.ThreadPoolExecutor(4) as ex: list(ex.map(lambda ix: send(ix, s), CFG))
        if (s // B) % 10 == 0: print("ingested", s, round(time.time() - t0), flush=True)
    for ix in CFG:
        t = time.time(); req("POST", f"/{ix}/_refresh"); c, j, _ = req("POST", f"/{ix}/_forcemerge", max_num_segments=1, wait_for_completion="false"); print(ix, "forcemerge", c, round(time.time() - t), flush=True)
    out["ingest_seconds"] = time.time() - t0
    out["stats"] = {ix: req("GET", f"/{ix}/_stats/store,docs,segments")[1]["_all"]["primaries"] for ix in CFG}
    save(f"E6_ingest_{PFX}.json", out); print("done")
elif phase == "query":
    V = np.load(f"{H}/{PFX}_vecs.npy"); G = np.load(f"{H}/{PFX}_grp.npy"); Q = vecs(777, 200)
    sims = Q @ V.T
    thr = {"100%": None, "50%": 500, "5%": 50, "0.5%": 5}
    truth = {}
    for name, t in thr.items():
        s = sims.copy()
        if t is not None: s[:, G >= t] = -9
        truth[name] = np.argsort(-s, axis=1)[:, :10]
    out = {"mode": MODE, "rescore_oversample": RESCORE, "workload": "200 queries (random unit vectors, seed 777), k=10, single shard force-merged to 1 segment, sequential client on t2-dev; truth = numpy exact cosine with the filter; synthetic data", "cells": []}
    for ix, (typ, heur) in CFG.items():
        for nc in (100, 500):
            for name, t in thr.items():
                rec, ts, tk = [], [], []
                for qi in range(200):
                    knn = {"field": "v", "query_vector": Q[qi].tolist(), "k": 10, "num_candidates": nc}
                    if t is not None: knn["filter"] = {"range": {"grp": {"lt": t}}}
                    if RESCORE: knn["rescore_vector"] = {"oversample": RESCORE}
                    c, j, ms = req("POST", f"/{ix}/_search", {"knn": knn, "size": 10, "_source": False, "fields": []})
                    if c != 200: out["cells"].append({"ix": ix, "err": cause(j)}); break
                    got = {int(h["_id"]) for h in j["hits"]["hits"]}; rec.append(len(got & set(truth[name][qi].tolist())) / 10)
                    if qi >= 10: ts.append(ms); tk.append(j["took"])
                else:
                    q = statistics.quantiles(ts, n=100)
                    out["cells"].append({"index": typ, "heuristic": heur, "num_candidates": nc, "filter_pass": name, "recall_at_10": statistics.mean(rec), "client_ms_p50": q[49], "client_ms_p90": q[89], "took_ms_p50": statistics.median(tk)})
                    print(out["cells"][-1], flush=True)
    # exact search latency (script_score) on bbq index, filtered
    ex = {}
    for name, t in thr.items():
        ts = []
        for qi in range(20):
            f = {"match_all": {}} if t is None else {"range": {"grp": {"lt": t}}}
            c, j, ms = req("POST", f"/{PFX}-int8-acorn/_search", {"size": 10, "_source": False, "query": {"script_score": {"query": {"bool": {"filter": [f]}}, "script": {"source": "cosineSimilarity(params.q, 'v') + 1.0", "params": {"q": Q[qi].tolist()}}}}}); ts.append(ms)
        ex[name] = {"client_ms_p50": statistics.median(ts), "status": c, "n": 20}
    out["exact_script_score_latency_int8_index"] = ex
    save("E6_query.json" if PFX == "e6" and not RESCORE else f"E6_query_{PFX}" + (f"_rescore{RESCORE:g}" if RESCORE else "") + ".json", out); print("done")
elif phase == "bbqdisk":
    out = {}
    ix = "e6-bbqdisk"; req("DELETE", f"/{ix}")
    c, j, _ = req("PUT", f"/{ix}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"v": {"type": "dense_vector", "dims": DIMS, "similarity": "cosine", "index_options": {"type": "bbq_disk"}}}}})
    out["1_put_mapping"] = {"status": c, **(cause(j) if c >= 300 else {"ok": True})}
    V = vecs(5, 1100)
    nd = "".join('{"index":{"_id":"%d"}}\n%s\n' % (n, json.dumps({"v": V[n].tolist()})) for n in range(1100))
    c, j, _ = req("POST", f"/{ix}/_bulk", ndjson=nd); out["2_bulk_index"] = {"status": c, "errors": j.get("errors"), "first_item": (j.get("items") or [{}])[0] if j.get("errors") else "ok"}
    c, j, _ = req("POST", f"/{ix}/_refresh"); out["3_refresh"] = {"status": c, "shards": j.get("_shards")}
    c, j, _ = req("POST", f"/{ix}/_flush"); out["4_flush"] = {"status": c, "shards": j.get("_shards")}
    c, j, _ = req("POST", f"/{ix}/_search", {"knn": {"field": "v", "query_vector": V[0].tolist(), "k": 3, "num_candidates": 20}, "_source": False}); out["5_search"] = {"status": c, "hits": len(j.get("hits", {}).get("hits", [])) if c == 200 else cause(j)}
    out["6_health"] = req("GET", f"/_cluster/health/{ix}")[1].get("status"); out["license"] = req("GET", "/_license")[1]["license"]["type"]
    save(f"E6_bbqdisk_{sys.argv[2]}.json", out); print(json.dumps(out, indent=1))
