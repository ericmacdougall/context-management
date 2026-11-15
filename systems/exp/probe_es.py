"""Probe what the scoped key may do on sys-probe-* (report, never work around)."""
import json
import sys
sys.path.insert(0, ".")
from memsys.esc import ES

es = ES()
out = {}
def t(name, *a, **k):
    try:
        s, j = es.req(*a, ok=None, **k)
        out[name] = (s, (j.get("error", {}).get("reason") if isinstance(j, dict) and "error" in j else "ok"))
    except Exception as e:
        out[name] = ("exc", str(e)[:150])
    print(name, out[name], flush=True)
    return j if "j" in dir() else None

for i in ("sys-probe-a-g1", "sys-probe-a-g2"):
    es.req("DELETE", f"/{i}", ok=None)
t("template_put", "PUT", "/_index_template/sys-probe-tpl", {"index_patterns": ["sys-probe-*"], "template": {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"v": {"type": "dense_vector", "dims": 4, "similarity": "cosine", "index_options": {"type": "int8_hnsw"}}, "t": {"type": "text"}}}}})
t("create_index_with_aliases", "PUT", "/sys-probe-a-g1", {"aliases": {"sys-probe-w": {}, "sys-probe-r": {}}})
_, j = es.req("POST", "/_bulk", ndjson=json.dumps({"index": {"_index": "sys-probe-w", "_id": "1"}}) + "\n" + json.dumps({"t": "hello", "v": [1, 0, 0, 0]}) + "\n", ok=None, require_alias=True)
print("bulk via alias require_alias", j.get("errors"))
t("refresh", "POST", "/sys-probe-a-g1/_refresh")
t("bulk_nonexistent_require_alias", "POST", "/_bulk", ndjson=json.dumps({"index": {"_index": "sys-probe-nonexist", "_id": "1"}}) + "\n" + json.dumps({"t": "x"}) + "\n", require_alias=True)
t("pit_open", "POST", "/sys-probe-r/_pit", keep_alive="1m")
t("search_knn", "POST", "/sys-probe-r/_search", {"knn": {"field": "v", "query_vector": [1, 0, 0, 0], "k": 1, "num_candidates": 10}})
t("script_score", "POST", "/sys-probe-r/_search", {"query": {"script_score": {"query": {"match_all": {}}, "script": {"source": "cosineSimilarity(params.q, 'v') + 1.0", "params": {"q": [1, 0, 0, 0]}}}}})
t("create_g2", "PUT", "/sys-probe-a-g2")
t("aliases_swap_remove_index", "POST", "/_aliases", {"actions": [{"remove": {"index": "sys-probe-a-g1", "alias": "sys-probe-w"}}, {"add": {"index": "sys-probe-a-g2", "alias": "sys-probe-w", "is_write_index": True}}, {"remove": {"index": "sys-probe-a-g1", "alias": "sys-probe-r"}}, {"add": {"index": "sys-probe-a-g2", "alias": "sys-probe-r"}}, {"remove_index": {"index": "sys-probe-a-g1"}}]})
t("flush", "POST", "/sys-probe-a-g2/_flush")
t("settings_get", "GET", "/sys-probe-a-g2/_settings")
t("stats", "GET", "/sys-probe-a-g2/_stats")
t("cat_indices_sys", "GET", "/_cat/indices/sys-*")
t("delete_by_query", "POST", "/sys-probe-a-g2/_delete_by_query", {"query": {"match_all": {}}})
es.req("DELETE", "/sys-probe-a-g2", ok=None)
t("template_delete", "DELETE", "/_index_template/sys-probe-tpl")
json.dump(out, open("results/probe_es.json", "w"), indent=1) if False else None
