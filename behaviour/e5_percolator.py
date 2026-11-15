import random, statistics, sys
from esc import *
phase = sys.argv[1] if len(sys.argv) > 1 else "basic"
out = {"phase": phase}
def short(c, j): return {"status": c, "result": j.get("result") or j.get("error", {}).get("reason") or j.get("error"), "type": j.get("error", {}).get("type") if isinstance(j.get("error"), dict) else None}
def cause(j):
    e = j.get("error", {}); rc = e.get("root_cause", [{}])[0].get("reason") if isinstance(e, dict) else None
    return {"type": e.get("type"), "reason": e.get("reason"), "root_cause": rc, "caused_by": (e.get("caused_by") or {}).get("reason")}
if phase == "basic":
    IDX = "e5-perc"; req("DELETE", f"/{IDX}")
    c, j, _ = req("PUT", f"/{IDX}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"q": {"type": "percolator"}, "body": {"type": "text"}, "vec": {"type": "dense_vector", "dims": 3, "index": True, "similarity": "cosine"}, "sp": {"type": "sparse_vector"}}}})
    out["create_index"] = (c, j.get("acknowledged") or cause(j))
    qs = {"m1": {"match": {"body": "refund policy"}}, "t1": {"term": {"body": "paris"}}, "b1": {"bool": {"must": [{"match": {"body": "dentist"}}], "must_not": [{"match": {"body": "cancelled"}}]}}, "p1": {"match_phrase": {"body": "moved to berlin"}}}
    for k, q in qs.items(): req("PUT", f"/{IDX}/_doc/{k}", {"q": q}, refresh="true")
    c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"percolate": {"field": "q", "document": {"body": "Customer asked about the refund policy and mentioned Paris"}}}})
    out["single_doc_matches"] = [h["_id"] for h in j["hits"]["hits"]]
    docs = [{"body": "dentist appointment tomorrow"}, {"body": "dentist appointment cancelled"}, {"body": "she moved to Berlin last year"}, {"body": "nothing relevant"}]
    c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"percolate": {"field": "q", "documents": docs}}})
    out["multi_doc_slots"] = [{"id": h["_id"], "slots": h["fields"]["_percolator_document_slot"]} for h in j["hits"]["hits"]]
    # unsupported stored queries
    tests = {"knn": {"knn": {"field": "vec", "query_vector": [0.1, 0.2, 0.3], "k": 3, "num_candidates": 10}},
             "sparse_vector": {"sparse_vector": {"field": "sp", "query_vector": {"a": 1.0, "b": 0.5}}},
             "sparse_vector_inference": {"sparse_vector": {"field": "sp", "inference_id": "nope", "query": "x"}},
             "script_score_cosine": {"script_score": {"query": {"match_all": {}}, "script": {"source": "cosineSimilarity(params.v, 'vec') + 1.0", "params": {"v": [0.1, 0.2, 0.3]}}}},
             "more_like_this": {"more_like_this": {"fields": ["body"], "like": "hello world", "min_term_freq": 1, "min_doc_freq": 1}},
             "has_child_like_terms_lookup": {"terms": {"body": {"index": "x", "id": "1", "path": "p"}}},
             "range_now": {"range": {"body": {"gte": "now"}}}}
    out["stored_unsupported"] = {}
    for k, q in tests.items():
        c, j, _ = req("PUT", f"/{IDX}/_doc/u_{k}", {"q": q}, refresh="true"); out["stored_unsupported"][k] = {"status": c, **(cause(j) if c >= 300 else {"result": j.get("result")})}
    # match a doc against stored sparse_vector query if accepted
    # latency
    random.seed(7); vocab = [f"tok{n}" for n in range(5000)]
    lat = {}
    for N in (1000, 10000, 100000):
        I = f"e5-lat-{N}"; req("DELETE", f"/{I}")
        req("PUT", f"/{I}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"}, "mappings": {"properties": {"q": {"type": "percolator"}, "body": {"type": "text"}}}})
        for s in range(0, N, 5000):
            nd = "".join(json.dumps({"index": {"_id": f"q{n}"}}) + "\n" + json.dumps({"q": {"term": {"body": random.choice(vocab)}}}) + "\n" for n in range(s, min(N, s + 5000)))
            c, j, _ = req("POST", f"/{I}/_bulk", ndjson=nd); assert not j["errors"], j["items"][0]
        req("POST", f"/{I}/_refresh")
        doc = {"body": " ".join(random.sample(vocab, 30))}
        ts, took = [], []
        for r in range(220):
            c, j, ms = req("POST", f"/{I}/_search", {"query": {"percolate": {"field": "q", "document": doc}}, "size": 10}); ts.append(ms); took.append(j["took"])
        ts, took = ts[20:], took[20:]; q = statistics.quantiles(ts, n=100); qt = statistics.quantiles(took, n=100)
        docs10 = [{"body": " ".join(random.sample(vocab, 30))} for _ in range(10)]
        t10 = []
        for r in range(100): t10.append(req("POST", f"/{I}/_search", {"query": {"percolate": {"field": "q", "documents": docs10}}, "size": 10})[2])
        q10 = statistics.quantiles(t10, n=100)
        lat[N] = {"matches_for_doc": req("POST", f"/{I}/_search", {"query": {"percolate": {"field": "q", "document": doc}}, "size": 0, "track_total_hits": True})[1]["hits"]["total"]["value"],
                  "one_doc_client_ms": {"p50": q[49], "p90": q[89], "p99": q[98]}, "one_doc_took_ms": {"p50": qt[49], "p90": qt[89]}, "ten_docs_client_ms": {"p50": q10[49], "p90": q10[89]}}
    out["latency_term_queries"] = {"workload": "N stored term queries over 5000-token vocab (random), 1 shard, query doc = 30 random tokens, 200 reps after 20 warmup, sequential client on same host; 10-doc variant 100 reps", "results": lat}
save(f"E5_percolator_{phase}.json", out); print(json.dumps(out, indent=1))
