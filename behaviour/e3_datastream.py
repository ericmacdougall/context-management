from esc import *
def put(c, j, _=None): return (c, j.get("result") or j.get("error", {}).get("type") or j.get("error"), j.get("_index"))
req("DELETE", "/_data_stream/e3-events"); req("DELETE", "/_index_template/e3-events")
c, j, _ = req("PUT", "/_index_template/e3-events", {"index_patterns": ["e3-events"], "data_stream": {}, "priority": 500,
   "template": {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"@timestamp": {"type": "date"}, "msg": {"type": "text"}}}}})
out = {"template": j}
out["create_ds"] = req("PUT", "/_data_stream/e3-events")[1]
r = {}
r["first_create"] = put(*req("PUT", "/e3-events/_create/ev-1", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "orig"}))
r["retry_same_backing_index"] = put(*req("PUT", "/e3-events/_create/ev-1", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "retry"}))
r["op_type_index_on_stream"] = put(*req("PUT", "/e3-events/_doc/ev-x", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "x"}))
r["post_no_id_no_create"] = put(*req("POST", "/e3-events/_doc", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "x"}))
# backfill old timestamp
r["backfill_2020"] = put(*req("PUT", "/e3-events/_create/ev-old", {"@timestamp": "2020-01-01T00:00:00Z", "msg": "old"}))
# update / delete directly on stream
r["update_on_stream"] = put(*req("POST", "/e3-events/_update/ev-1", {"doc": {"msg": "u"}}))
r["delete_on_stream"] = put(*req("DELETE", "/e3-events/_doc/ev-1"))
r["index_overwrite_on_stream_with_if_seq"] = put(*req("PUT", "/e3-events/_doc/ev-1", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "ow"}, if_seq_no=0, if_primary_term=1))
bk = req("GET", "/_data_stream/e3-events")[1]["data_streams"][0]["indices"][0]["index_name"]
r["backing_index_1"] = bk
d1 = req("PUT", f"/{bk}/_doc/ev-1", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "direct-overwrite-on-backing-index"})
r["overwrite_via_backing_index_as_index_op"] = put(*d1)
r["update_via_backing_index"] = put(*req("POST", f"/{bk}/_update/ev-1", {"doc": {"msg": "upd-backing"}}))
r["bulk_on_stream_update_delete"] = None
nd = "\n".join(json.dumps(x) for x in [{"update": {"_index": "e3-events", "_id": "ev-1"}}, {"doc": {"msg": "b"}}, {"delete": {"_index": "e3-events", "_id": "ev-old"}}]) + "\n"
c, j, _ = req("POST", "/_bulk", ndjson=nd); r["bulk_on_stream_update_delete"] = [{k: (v["status"], v.get("error", {}).get("type"), v.get("error", {}).get("reason", "")[:160])} for it in j["items"] for k, v in it.items()]
# rollover, then retry same id
out["rollover"] = req("POST", "/e3-events/_rollover")[1]
r["retry_after_rollover"] = put(*req("PUT", "/e3-events/_create/ev-1", {"@timestamp": "2026-11-01T00:00:00Z", "msg": "retry-after-rollover"}))
r["backfill_old_after_rollover"] = put(*req("PUT", "/e3-events/_create/ev-old2", {"@timestamp": "2019-01-01T00:00:00Z", "msg": "old2"}))
req("POST", "/e3-events/_refresh")
h = req("POST", "/e3-events/_search", {"query": {"ids": {"values": ["ev-1", "ev-old", "ev-old2"]}}, "size": 10, "_source": ["@timestamp", "msg"]})[1]["hits"]["hits"]
r["docs_found"] = [{"_id": x["_id"], "_index": x["_index"], "msg": x["_source"]["msg"]} for x in h]
r["duplicate_ev1_across_backing_indices"] = sum(1 for x in h if x["_id"] == "ev-1")
r["data_stream_info"] = [i["index_name"] for i in req("GET", "/_data_stream/e3-events")[1]["data_streams"][0]["indices"]]
out["results"] = r
save("E3_datastream.json", out); print(json.dumps(out, indent=1))
