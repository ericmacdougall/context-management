"""Probe the Elasticsearch 9.5 behaviours the fence depends on. Each line is a named expectation and its observed result.
P1 require_alias rejects a concrete index name.   P2 require_alias rejects a missing alias (no auto-create).
P3 an _aliases request whose `remove` has must_exist=true fails as a whole when the alias is absent (no partial apply).
P4 the same request succeeds when the alias is present, and moves both aliases atomically.
P5 external versioning rejects a lower version (409).  P6 a delete by an older external version is rejected.
P7 action.auto_create_index=false blocks a plain index request without require_alias (deployment setting, declared).
P9-P12 per-document erasure fence (placeholders): a content-free placeholder at the same _id with external version 2^62
refuses a later stale `create` (events) and a stale external_gte write (facts) with 409, in either order; with
index.merge.policy.expunge_deletes_allowed=0, refresh + _forcemerge?only_expunge_deletes + flush leaves docs.deleted=0
and the overwritten marker in 0 files on disk (control: same without the expunge finds it)."""
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
BIGV = 2 ** 62
W2 = f"{P}-w2"
es.bulk_fenced([({"create": {"_index": W2, "_id": "ev1"}}, {"content": "x"})])
out["P9_placeholder_over_create"] = es.bulk_fenced([({"index": {"_index": W2, "_id": "ev1", "version": BIGV, "version_type": "external"}}, {"erased": True})])
out["P9_stale_create_after"] = es.bulk_fenced([({"create": {"_index": W2, "_id": "ev1"}}, {"content": "stale"})])[1]
out["P9_stale_external_after"] = es.bulk_fenced([({"index": {"_index": W2, "_id": "ev1", "version": 9, "version_type": "external_gte"}}, {"content": "stale"})])[1]
out["P10_placeholder_first"] = es.bulk_fenced([({"index": {"_index": W2, "_id": "ev2", "version": BIGV, "version_type": "external"}}, {"erased": True})])
out["P10_stale_create"] = es.bulk_fenced([({"create": {"_index": W2, "_id": "ev2"}}, {"content": "stale"})])[1]
es.bulk_fenced([({"index": {"_index": W2, "_id": "f1", "version": 7, "version_type": "external_gte"}}, {"value": "x"})])
out["P11_placeholder_over_fact"] = es.bulk_fenced([({"index": {"_index": W2, "_id": "f1", "version": BIGV, "version_type": "external"}}, {"erased": True})])
out["P11_stale_fact_rev8"] = es.bulk_fenced([({"index": {"_index": W2, "_id": "f1", "version": 8, "version_type": "external_gte"}}, {"value": "stale"})])[1]
es.req("POST", f"/{W2}/_refresh")
out["P9_P11_sources_are_placeholders"] = all(es.req("GET", f"/{P}-b/_doc/{d}")[1].get("_source") == {"erased": True} for d in ("ev1", "ev2", "f1"))


def p12(expunge):
    from exp.registry_marker import grep_files, ES_C
    import uuid as _u
    idx = f"{P}-x{'e' if expunge else 'n'}"
    es.req("PUT", f"/{idx}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "index.merge.policy.expunge_deletes_allowed": 0},
                              "aliases": {f"{idx}-w": {"is_write_index": True}}})
    mk = "zqp12" + _u.uuid4().hex[:10]
    es.bulk_fenced([({"create": {"_index": f"{idx}-w", "_id": f"d{i}"}}, {"content": mk if i == 0 else f"filler {i}"}) for i in range(200)])
    es.req("POST", f"/{idx}/_refresh"); es.req("POST", f"/{idx}/_flush")
    before = grep_files(ES_C, "/usr/share/elasticsearch/data", mk)
    es.bulk_fenced([({"index": {"_index": f"{idx}-w", "_id": "d0", "version": BIGV, "version_type": "external"}}, {"erased": True})])
    es.req("POST", f"/{idx}/_refresh")
    if expunge:
        es.req("POST", f"/{idx}/_forcemerge", only_expunge_deletes=True)
    es.req("POST", f"/{idx}/_flush")
    st = es.req("GET", f"/{idx}/_stats/docs")[1]["_all"]["primaries"]["docs"]
    return {"files_before": before, "files_after": grep_files(ES_C, "/usr/share/elasticsearch/data", mk), "docs_deleted": st["deleted"], "docs_count": st["count"]}


out["P12_expunge"] = p12(True)
out["P12_control_no_expunge"] = p12(False)
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
      and out["P7_autocreate_plain_put"][0] >= 400 and out["P8_request_failed"] and out["P8_index_survived"] and out["P8_alias_not_added"]
      and out["P9_placeholder_over_create"][0] == 1 and out["P9_stale_create_after"] and out["P9_stale_create_after"][0][1] == 409
      and out["P9_stale_external_after"] and out["P9_stale_external_after"][0][1] == 409 and out["P10_stale_create"] and out["P10_stale_create"][0][1] == 409
      and out["P11_placeholder_over_fact"][0] == 1 and out["P11_stale_fact_rev8"] and out["P11_stale_fact_rev8"][0][1] == 409
      and out["P9_P11_sources_are_placeholders"]
      and out["P12_expunge"]["files_before"] > 0 and out["P12_expunge"]["files_after"] == 0 and out["P12_expunge"]["docs_deleted"] == 0
      and out["P12_control_no_expunge"]["files_after"] > 0)
out["pass"] = bool(ok)
print(json.dumps(out, indent=1, default=str))
write_result("es_semantics", out)
sys.exit(0 if ok else 1)
