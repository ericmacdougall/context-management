from esc import *
out = {}
for idx, props in [("e4-dates", {"valid_from": {"type": "date"}, "valid_to": {"type": "date"}}), ("e4-range", {"valid": {"type": "date_range"}})]:
    req("DELETE", f"/{idx}"); out[idx + "_create"] = req("PUT", f"/{idx}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {**props, "id": {"type": "keyword"}}}})[1]
# facts: (id, from, to) ; to None = open ended
F = [("f1", "2024-01-01", "2024-06-30"), ("f2", "2024-07-01", "2025-12-31"), ("f3", "2026-01-01", None), ("f4", "2023-01-01", None), ("f5", "2025-01-01", "2026-12-31")]
for i, a, b in F:
    d = {"id": i, "valid_from": a}; 
    if b: d["valid_to"] = b
    req("PUT", f"/e4-dates/_doc/{i}", d)
    rg = {"gte": a}
    if b: rg["lt"] = b
    r1 = req("PUT", f"/e4-range/_doc/{i}", {"id": i, "valid": rg})
    if r1[0] >= 300: out.setdefault("range_index_errors", []).append((i, r1[1].get("error", {}).get("reason")))
    if i == "f3":  # also try explicit null bound
        r2 = req("PUT", "/e4-range/_doc/f3null", {"id": "f3null", "valid": {"gte": a, "lt": None}}); out["range_explicit_null_lt"] = (r2[0], r2[1].get("result") or r2[1].get("error", {}).get("reason"))
req("POST", "/_all/_refresh")
def ids(idx, q, sort=None):
    b = {"query": q, "size": 20, "_source": ["id"]}; 
    if sort: b["sort"] = sort
    c, j, _ = req("POST", f"/{idx}/_search", b)
    return [x["_id"] for x in j["hits"]["hits"]] if c == 200 else {"status": c, "err": j.get("error", {}).get("root_cause", [{}])[0].get("reason", j.get("error"))}
T = "2025-03-01"
out["asof_2025-03-01"] = {
 "dates": ids("e4-dates", {"bool": {"filter": [{"range": {"valid_from": {"lte": T}}}, {"bool": {"should": [{"bool": {"must_not": {"exists": {"field": "valid_to"}}}}, {"range": {"valid_to": {"gt": T}}}], "minimum_should_match": 1}}]}}),
 "range_term": ids("e4-range", {"term": {"valid": T}}),
 "range_CONTAINS": ids("e4-range", {"range": {"valid": {"gte": T, "lte": T, "relation": "contains"}}}),
 "expected": ["f2", "f4", "f5"]}
out["currently_valid_now(2026-11-15)"] = {
 "dates_now": ids("e4-dates", {"bool": {"filter": [{"range": {"valid_from": {"lte": "now"}}}, {"bool": {"should": [{"bool": {"must_not": {"exists": {"field": "valid_to"}}}}, {"range": {"valid_to": {"gt": "now"}}}], "minimum_should_match": 1}}]}}),
 "range_now": ids("e4-range", {"term": {"valid": "now"}}), "expected": ["f3", "f4", "f5"]}
out["window_overlap_2024-06-01..2024-08-01"] = {
 "INTERSECTS": ids("e4-range", {"range": {"valid": {"gte": "2024-06-01", "lte": "2024-08-01", "relation": "intersects"}}}),
 "WITHIN": ids("e4-range", {"range": {"valid": {"gte": "2023-01-01", "lt": "2025-01-01", "relation": "within"}}}),
 "CONTAINS": ids("e4-range", {"range": {"valid": {"gte": "2024-02-01", "lte": "2024-03-01", "relation": "contains"}}})}
out["sort_valid_from_dates_asc"] = ids("e4-dates", {"match_all": {}}, [{"valid_from": "asc"}])
out["sort_valid_from_dates_desc_missing_valid_to_sort"] = ids("e4-dates", {"match_all": {}}, [{"valid_to": {"order": "asc", "missing": "_last"}}])
out["sort_range_field"] = ids("e4-range", {"match_all": {}}, [{"valid": "asc"}])
c, j, _ = req("POST", "/e4-range/_search", {"query": {"match_all": {}}, "docvalue_fields": ["valid"], "size": 3}); out["range_docvalue_fields"] = (c, [x.get("fields") for x in j.get("hits", {}).get("hits", [])] or j)
# script sort on range lower bound
c, j, _ = req("POST", "/e4-range/_search", {"query": {"match_all": {}}, "size": 3, "sort": [{"_script": {"type": "number", "order": "asc", "script": {"source": "doc['valid'].size()"}}}]}); out["script_sort_on_range_doc_values"] = (c, j.get("error", {}).get("root_cause", [{}])[0].get("reason") if c != 200 else "ok")
c, j, _ = req("PUT", "/e4-range/_doc/now1", {"id": "now1", "valid": {"gte": "now", "lt": "2030-01-01"}}); out["stored_bound_now"] = (c, j.get("result") or j.get("error", {}).get("reason"), (j.get("error", {}).get("caused_by") or {}).get("reason"))
c, j, _ = req("PUT", "/e4-range/_doc/now2", {"id": "now2", "valid": {"gte": "now-1d/d", "lt": "2030-01-01"}}); out["stored_bound_now_math"] = (c, j.get("result") or j.get("error", {}).get("reason"), (j.get("error", {}).get("caused_by") or {}).get("reason"))
req("POST", "/e4-range/_refresh")
out["range_default_relation_ids_for_window"] = ids("e4-range", {"range": {"valid": {"gte": "2024-06-01", "lte": "2024-08-01"}}})
out["range_docvalue_note"] = "see range_docvalue_fields"
save("E4_validity.json", out); print(json.dumps(out, indent=1))
