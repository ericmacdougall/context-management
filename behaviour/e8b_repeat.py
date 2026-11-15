from esc import *
import importlib.util, sys
sys.argv = ["x", "none"]
MODEL = ".multilingual-e5-small"
def counts():
    t = req("GET", f"/_ml/trained_models/{MODEL}/_stats")[1]["trained_model_stats"][0]
    return t["inference_stats"]["inference_count"], sum(n.get("inference_count", 0) for n in t["deployment_stats"]["nodes"])
IDX = "e8b-sem"; req("DELETE", f"/{IDX}")
req("PUT", f"/{IDX}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"tag": {"type": "keyword"}, "body": {"type": "semantic_text", "inference_id": "emlab-e5"}}}})
N = 10
nd = "".join(json.dumps({"index": {"_index": IDX, "_id": f"d{n}"}}) + "\n" + json.dumps({"tag": "t0", "body": f"Document {n} about travel to Berlin and Paris, dentist visits and refunds."}) + "\n" for n in range(N))
req("POST", "/_bulk", ndjson=nd, refresh="true"); time.sleep(1)
out = {"n_docs": N, "note": "counts = (inference_stats.inference_count, summed deployment inference_count)"}
def measure(name, fn):
    b = counts(); fn(); time.sleep(1.5); a = counts(); out[name] = {"delta": [a[0] - b[0], a[1] - b[1]]}; print(name, out[name], flush=True)
def bulk_upd(tag): 
    nd = "".join(json.dumps({"update": {"_index": IDX, "_id": f"d{n}"}}) + "\n" + json.dumps({"doc": {"tag": tag}}) + "\n" for n in range(N)); c, j, _ = req("POST", "/_bulk", ndjson=nd, refresh="true"); out.setdefault("bulk_errors", []).append(j["errors"])
def single_upd(tag):
    for n in range(N): req("POST", f"/{IDX}/_update/d{n}", {"doc": {"tag": tag}})
    req("POST", f"/{IDX}/_refresh")
measure("bulk_partial_update_10docs_run1", lambda: bulk_upd("b1"))
measure("bulk_partial_update_10docs_run2", lambda: bulk_upd("b2"))
measure("update_api_doc_partial_10calls_run1", lambda: single_upd("s1"))
measure("update_api_doc_partial_10calls_run2", lambda: single_upd("s2"))
measure("bulk_partial_update_10docs_run3_after_single", lambda: bulk_upd("b3"))
# does bulk partial update keep semantic results intact?
c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"semantic": {"field": "body", "query": "dentist"}}, "size": 3, "_source": ["tag"]}); out["semantic_query_after"] = [(h["_id"], h["_score"], h["_source"]["tag"]) for h in j["hits"]["hits"]]
# doc_as_upsert / noop detect
measure("update_api_noop_same_tag", lambda: req("POST", f"/{IDX}/_update/d0", {"doc": {"tag": "b3"}}))
measure("bulk_update_noop_same_tag", lambda: req("POST", "/_bulk", ndjson=json.dumps({"update": {"_index": IDX, "_id": "d0"}}) + "\n" + json.dumps({"doc": {"tag": "b3"}}) + "\n", refresh="true"))
save("E8b_repeat.json", out)
