import sys, random
import requests
from esc import *
phase = sys.argv[1]; IDX = "e9-shared"; adm = admin_session()
def cause(j):
    e = j.get("error", {}); return {"type": e.get("type"), "reason": e.get("reason"), "root": (e.get("root_cause") or [{}])[0].get("reason")} if isinstance(e, dict) else j
def mkkey(tenant):
    c, j, _ = req("POST", "/_security/api_key", {"name": f"emlab-e9-{tenant}", "role_descriptors": {"r": {"indices": [{"names": [IDX], "privileges": ["read"], "query": {"term": {"tenant": tenant}}}]}}}, sess=adm)
    if c != 200: return None, {"status": c, **cause(j)}
    s = requests.Session(); s.verify = S.verify; s.headers.update({"Authorization": "ApiKey " + j["encoded"], "Content-Type": "application/json"}); return s, {"status": c, "created": True}
out = {"phase": phase, "license": req("GET", "/_license")[1]["license"]["type"]}
random.seed(3)
req("DELETE", f"/{IDX}"); req("PUT", f"/{IDX}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"tenant": {"type": "keyword"}, "body": {"type": "text"}, "cat": {"type": "keyword"}, "vec": {"type": "dense_vector", "dims": 3, "index": True, "similarity": "cosine"}}}})
nd = ""
for n in range(30):
    nd += json.dumps({"index": {"_id": f"a{n}"}}) + "\n" + json.dumps({"tenant": "A", "body": "kiwi bird facts" if n < 6 else "other content here", "cat": "a-cat" if n % 2 else "shared-cat", "vec": [random.random(), 1.0, random.random()]}) + "\n"
for n in range(30):
    nd += json.dumps({"index": {"_id": f"b0{n}"}}) + "\n" + json.dumps({"tenant": "B", "body": "other b content", "cat": "b-secret-cat", "vec": [random.random(), 1.0, random.random()]}) + "\n"
req("POST", f"/{IDX}/_bulk", ndjson=nd, refresh="true")
kA, out["create_key_A"] = mkkey("A"); kB, out["create_key_B"] = mkkey("B")
def srch(sess, body, name):
    c, j, _ = req("POST", f"/{IDX}/_search", body, sess=sess)
    return (c, j) if c == 200 else (c, cause(j))
if kA is None: save(f"E9_dls_{phase}.json", out); print(json.dumps(out, indent=1)); sys.exit()
def run(tag):
    r = {}
    for nm, s in (("A", kA), ("B", kB)):
        c, j = srch(s, {"query": {"match_all": {}}, "size": 0, "track_total_hits": True, "aggs": {"cats": {"terms": {"field": "cat", "size": 10}}, "tenants": {"terms": {"field": "tenant"}}}}, nm)
        if c != 200: r[f"{nm}_match_all"] = {"status": c, **j}; continue
        r[f"{nm}_match_all"] = {"total": j["hits"]["total"]["value"], "tenant_buckets": j["aggregations"]["tenants"]["buckets"], "cat_buckets": j["aggregations"]["cats"]["buckets"]}
    c, j = srch(kA, {"query": {"match": {"body": "kiwi"}}, "size": 3, "explain": False}, "A")
    r["A_kiwi"] = {"status": c, "hits": [(h["_id"], h["_score"]) for h in j["hits"]["hits"]] if c == 200 else j}
    r["A_idf_via_explain"] = None
    c, j = srch(kA, {"query": {"match": {"body": "kiwi"}}, "size": 1, "explain": True}, "A")
    if c == 200 and j["hits"]["hits"]:
        def walk(d, acc):
            if "idf" in d.get("description", "") or d.get("description", "").startswith("idf"): acc.append((d["description"], d["value"], [x["description"] for x in d.get("details", [])]))
            for x in d.get("details", []): walk(x, acc)
        acc = []; walk(j["hits"]["hits"][0]["_explanation"], acc); r["A_idf_via_explain"] = acc[:2]
    adm_c, adm_j, _ = req("POST", f"/{IDX}/_search", {"query": {"match": {"body": "kiwi"}}, "size": 1}, sess=adm); r["admin_kiwi_total"] = adm_j["hits"]["total"]["value"]
    c, j = srch(kA, {"size": 0, "query": {"match_all": {}}, "aggs": {"sig": {"significant_terms": {"field": "cat", "background_filter": {"match_all": {}}, "min_doc_count": 1}}}}, "A")
    r["A_significant_terms"] = j["aggregations"]["sig"] if c == 200 else {"status": c, **j}
    c, j = srch(kA, {"knn": {"field": "vec", "query_vector": [0.0, 1.0, 0.0], "k": 5, "num_candidates": 10}, "size": 5, "_source": ["tenant"]}, "A")
    r["A_knn_k5"] = {"status": c, "n": len(j["hits"]["hits"]), "tenants": sorted({h["_source"]["tenant"] for h in j["hits"]["hits"]})} if c == 200 else j
    c, j, _ = req("POST", f"/{IDX}/_terms_enum", {"field": "cat", "string": "", "size": 10}, sess=kA); r["A_terms_enum_cat"] = j.get("terms") if c == 200 else {"status": c, **cause(j)}
    c, j, _ = req("GET", f"/{IDX}/_count", sess=kA); r["A_count_api"] = j.get("count") if c == 200 else {"status": c, **cause(j)}
    out[tag] = r
run("before_B_adds_docs")
# B adds 20k docs with the same term, vectors near query vector
nd = ""
for n in range(20000):
    nd += json.dumps({"index": {}}) + "\n" + json.dumps({"tenant": "B", "body": "kiwi kiwi tracking notes", "cat": "b-secret-cat", "vec": [0.001 * random.random(), 1.0, 0.001 * random.random()]}) + "\n"
    if (n + 1) % 5000 == 0: req("POST", f"/{IDX}/_bulk", ndjson=nd); nd = ""
req("POST", f"/{IDX}/_refresh")
run("after_B_adds_20000_kiwi_docs")
b = out["before_B_adds_docs"]["A_kiwi"]["hits"]; a = out["after_B_adds_20000_kiwi_docs"]["A_kiwi"]["hits"]
out["score_shift_A_top_hit"] = {"before": b[0] if isinstance(b, list) and b else b, "after": a[0] if isinstance(a, list) and a else a}
save(f"E9_dls_{phase}.json", out); print(json.dumps(out, indent=1))
