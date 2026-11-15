import requests
from esc import *
IDX = "e9c-sem"; adm = admin_session(); req("DELETE", f"/{IDX}")
req("PUT", f"/{IDX}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"tenant": {"type": "keyword"}, "body": {"type": "semantic_text", "inference_id": "emlab-e5"}}}})
nd = ""
for n in range(5): nd += json.dumps({"index": {"_id": f"a{n}"}}) + "\n" + json.dumps({"tenant": "A", "body": f"Notes about the weather and gardening number {n}"}) + "\n"
for n in range(20): nd += json.dumps({"index": {"_id": f"b{n}"}}) + "\n" + json.dumps({"tenant": "B", "body": f"Dentist appointment and tooth cleaning reminder number {n}"}) + "\n"
c, j, _ = req("POST", f"/{IDX}/_bulk", ndjson=nd, refresh="true"); out = {"bulk_errors": j["errors"]}
def key(t):
    c, j, _ = req("POST", "/_security/api_key", {"name": f"emlab-e9c-{t}", "role_descriptors": {"r": {"indices": [{"names": [IDX], "privileges": ["read"], "query": {"term": {"tenant": t}}}]}}}, sess=adm)
    s = requests.Session(); s.verify = S.verify; s.headers.update({"Authorization": "ApiKey " + j["encoded"], "Content-Type": "application/json"}); return s
kA = key("A")
Q = "dentist appointment"
for name, body in {"semantic_query": {"query": {"semantic": {"field": "body", "query": Q}}, "size": 5, "_source": ["tenant"]},
                   "match_on_semantic_text": {"query": {"match": {"body": Q}}, "size": 5, "_source": ["tenant"]},
                   "retriever_knn_style_semantic_in_bool": {"query": {"bool": {"must": [{"semantic": {"field": "body", "query": Q}}]}}, "size": 5, "_source": ["tenant"]}}.items():
    for who, s in (("admin", adm), ("tenantA", kA)):
        c, j, _ = req("POST", f"/{IDX}/_search", body, sess=s)
        out[f"{name}__{who}"] = {"status": c, "hits": [(h["_id"], round(h["_score"], 4), h["_source"]["tenant"]) for h in j["hits"]["hits"]]} if c == 200 else j
# explicit n / N for A's kiwi score in e9-shared
c, j, _ = req("POST", "/e9-shared/_search", {"query": {"match": {"body": "kiwi"}}, "size": 1, "explain": True}, sess=adm)
def walk(d, acc):
    if d.get("description", "").startswith("n, number") or d.get("description", "").startswith("N, total"): acc.append((d["description"], d["value"]))
    for x in d.get("details", []): walk(x, acc)
acc = []; walk(j["hits"]["hits"][0]["_explanation"], acc); out["admin_explain_n_N_e9_shared"] = acc
kk = requests.Session(); kk.verify = S.verify
c, j, _ = req("POST", "/_security/api_key", {"name": "emlab-e9c-A2", "role_descriptors": {"r": {"indices": [{"names": ["e9-shared"], "privileges": ["read"], "query": {"term": {"tenant": "A"}}}]}}}, sess=adm)
kk.headers.update({"Authorization": "ApiKey " + j["encoded"], "Content-Type": "application/json"})
c, j, _ = req("POST", "/e9-shared/_search", {"query": {"match": {"body": "kiwi"}}, "size": 1, "explain": True}, sess=kk)
acc = []; walk(j["hits"]["hits"][0]["_explanation"], acc); out["tenantA_explain_n_N_e9_shared"] = acc
save("E9c_dls_semantic.json", out); print(json.dumps(out, indent=1))
