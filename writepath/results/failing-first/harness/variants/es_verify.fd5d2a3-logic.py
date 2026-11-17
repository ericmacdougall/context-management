"""BROKEN VARIANT for failing-first evidence (D4): wp/es_verify.py with check_page reduced to the fd5d2a3 logic in
Verifier.ext_counts, i.e. count(ids filter, must_not erased:true) over the index pattern, answered through one copy
per shard (modelled as the primary). It reports the count under es:placeholder_content and nothing else."""
from . import families as F

PLACEHOLDER_SOURCE = {"erased": True}
KEYS = ("es:placeholder_content", "es:placeholder_version", "es:placeholder_missing", "es:copy_unverified")


def copies(es, pattern):
    s, j = es.req("GET", f"/_cat/shards/{pattern}", ok=(200, 404), format="json", h="index,shard,prirep,state,id",
                  expand_wildcards="all")
    if s != 200:
        return []
    return [(r["index"], str(r["shard"]), r["prirep"], r["state"], r.get("id")) for r in j]


def check_page(es, t, f, ids, cps=None, detail=None):
    out = dict.fromkeys(KEYS, 0)
    if not ids:
        return out
    cps = copies(es, f"{F.P}{t}-{f}-*") if cps is None else cps
    for index, shard, prirep, state, node in cps:
        if prirep != "p" or state != "STARTED":
            continue
        s, j = es.req("POST", f"/{index}/_search", {"query": {"ids": {"values": list(ids)}}, "size": len(ids)},
                      ok=(200,), preference=f"_shards:{shard}|_only_nodes:{node}")
        for h in j["hits"]["hits"]:
            if not (h.get("_source") or {}).get("erased"):
                out["es:placeholder_content"] += 1
    return out


def check(es, t, pages, derive_fact_id, detail=None):
    out = dict.fromkeys(KEYS, 0)
    for page in pages:
        for f, ids in (("events", [e for e, _ in page]), ("facts", [derive_fact_id(t, e) for e, k in page if k == "fact"])):
            for k, v in check_page(es, t, f, ids).items():
                out[k] += v
    return out
