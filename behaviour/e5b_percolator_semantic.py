from esc import *
IDX = "e5b-perc"; req("DELETE", f"/{IDX}")
def cause(j):
    e = j.get("error", {}); return {"type": e.get("type"), "reason": e.get("reason"), "root": (e.get("root_cause") or [{}])[0].get("reason"), "caused_by": (e.get("caused_by") or {}).get("reason")} if isinstance(e, dict) else j
out = {"license": req("GET", "/_license")[1]["license"]["type"]}
c, j, _ = req("PUT", f"/{IDX}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"q": {"type": "percolator"}, "body": {"type": "semantic_text", "inference_id": "emlab-e5"}, "txt": {"type": "text"}, "sp": {"type": "sparse_vector"}, "vec": {"type": "dense_vector", "dims": 3, "index": True, "similarity": "cosine"}}}})
out["create_index"] = (c, j.get("acknowledged") or cause(j))
tests = {"semantic": {"semantic": {"field": "body", "query": "dentist appointment"}},
         "match_on_semantic_text": {"match": {"body": "dentist appointment"}},
         "bool_filter_with_semantic": {"bool": {"filter": [{"match": {"txt": "dentist"}}], "must": [{"semantic": {"field": "body", "query": "dentist appointment"}}]}},
         "sparse_vector_query_vector": {"sparse_vector": {"field": "sp", "query_vector": {"a": 1.0, "b": 0.5}}},
         "sparse_vector_elser_inference_id": {"sparse_vector": {"field": "sp", "inference_id": ".elser-2-elasticsearch", "query": "dentist"}},
         "knn": {"knn": {"field": "vec", "query_vector": [0.1, 0.2, 0.3], "k": 3, "num_candidates": 10}},
         "match_txt_control": {"match": {"txt": "dentist"}}}
out["store"] = {}
for k, q in tests.items():
    c, j, _ = req("PUT", f"/{IDX}/_doc/{k}", {"q": q}, refresh="true"); out["store"][k] = {"status": c, **(cause(j) if c >= 300 else {"result": j.get("result")})}
req("POST", f"/{IDX}/_refresh")
docs = {"doc_with_text_and_body": {"txt": "dentist appointment tomorrow", "body": "dentist appointment tomorrow"}, "doc_body_only": {"body": "dentist appointment tomorrow"}}
out["percolate"] = {}
for dn, d in docs.items():
    c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"percolate": {"field": "q", "document": d}}, "size": 20})
    out["percolate"][dn] = {"status": c, "matches": [(h["_id"], h["_score"]) for h in j["hits"]["hits"]]} if c == 200 else {"status": c, **cause(j)}
save("E5_percolator_trial.json", out); print(json.dumps(out, indent=1))
