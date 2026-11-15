"""Probe the Elasticsearch 9.5 behaviours the fence depends on. Each line is a named expectation and its observed result.
P1 require_alias rejects a concrete index name.   P2 require_alias rejects a missing alias (no auto-create).
P3 an _aliases request whose `remove` has must_exist=true fails as a whole when the alias is absent (no partial apply).
P4 the same request succeeds when the alias is present, and moves both aliases atomically.
P5 external versioning rejects a lower version (409).  P6 a delete by an older external version is rejected.
P7 action.auto_create_index=false blocks a plain index request without require_alias (deployment setting, declared)."""
import json
import sys

from wp.es import ES, ESError
from exp.common import write_result

es = ES()
P = "wp16096-probe"
for i in es.indices(P + "-*"):
    es.req("DELETE", f"/{i}")
es.req("PUT", f"/{P}-a", {"aliases": {f"{P}-w1": {"is_write_index": True}, f"{P}-r": {}}})
es.req("PUT", f"/{P}-b")
out = {}


def bulk1(index, op="index", **meta):
    return es.bulk_fenced([({op: {"_index": index, "_id": "d1", **meta}}, {"x": 1} if op != "delete" else None)])


out["P1_concrete_name_rejected"] = bulk1(f"{P}-a")[1]
out["P2_missing_alias_rejected"] = bulk1(f"{P}-w9")[1]
out["P2b_index_count_after"] = sorted(es.indices(P + "-*"))
try:
    es.aliases([{"remove": {"index": f"{P}-a", "alias": f"{P}-w0", "must_exist": True}},
                {"add": {"index": f"{P}-b", "alias": f"{P}-r"}}])
    out["P3_must_exist_failed_whole"] = False
except ESError as e:
    out["P3_must_exist_failed_whole"] = e.etype
out["P3_r_alias_still_only_on_a"] = sorted(i for i, al in es.alias_map(P + "-*").items() if f"{P}-r" in al)
es.aliases([{"remove": {"index": f"{P}-a", "alias": f"{P}-w1", "must_exist": True}},
            {"add": {"index": f"{P}-b", "alias": f"{P}-w2", "is_write_index": True}},
            {"remove": {"index": f"{P}-a", "alias": f"{P}-r", "must_exist": True}},
            {"add": {"index": f"{P}-b", "alias": f"{P}-r"}}])
out["P4_aliases_after_move"] = {i: sorted(a) for i, a in es.alias_map(P + "-*").items()}
out["P4_old_w1_write"] = bulk1(f"{P}-w1")[1]
out["P5_v5"] = bulk1(f"{P}-w2", version=5, version_type="external")
out["P5_v3_rejected"] = bulk1(f"{P}-w2", version=3, version_type="external")[1]
out["P6_delete_v4_rejected"] = bulk1(f"{P}-w2", op="delete", version=4, version_type="external")[1]
# P8: remove_index combined with a failing must_exist remove: the index must survive (no partial apply)
es.req("PUT", f"/{P}-c", {"aliases": {f"{P}-rc": {}}})
try:
    es.aliases([{"remove_index": {"index": f"{P}-c"}}, {"remove": {"index": f"{P}-b", "alias": f"{P}-nope", "must_exist": True}},
                {"add": {"index": f"{P}-b", "alias": f"{P}-rc2"}}])
    out["P8_request_failed"] = False
except ESError as e:
    out["P8_request_failed"] = e.etype
out["P8_index_survived"] = f"{P}-c" in es.indices(P + "-*")
out["P8_alias_not_added"] = f"{P}-rc2" not in es.alias_map(P + "-b").get(f"{P}-b", set())
s, j = es.req("PUT", f"/{P}-zz/_doc/1", {"x": 1}, ok=None)
out["P7_autocreate_plain_put"] = [s, (j.get("error") or {}).get("type")]
out["P7_indices"] = sorted(es.indices(P + "-*"))
s, j = es.req("GET", "/_cluster/settings", include_defaults=True, filter_path="**.auto_create_index")
out["auto_create_index_setting"] = j
s, j = es.req("GET", "/_license")
out["license"] = j["license"]["type"]
for i in es.indices(P + "-*"):
    es.req("DELETE", f"/{i}")
ok = (out["P1_concrete_name_rejected"] and out["P2_missing_alias_rejected"] and out["P3_must_exist_failed_whole"]
      and out["P3_r_alias_still_only_on_a"] == [f"{P}-a"] and out["P4_old_w1_write"] and out["P5_v3_rejected"] and out["P6_delete_v4_rejected"]
      and out["P7_autocreate_plain_put"][0] >= 400 and out["P8_request_failed"] and out["P8_index_survived"] and out["P8_alias_not_added"])
out["pass"] = bool(ok)
print(json.dumps(out, indent=1, default=str))
write_result("es_semantics", out)
sys.exit(0 if ok else 1)
