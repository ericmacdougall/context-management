"""Elasticsearch projections: naming, templates, documents, index lifecycle.

Layout per tenant t (plain indices, 1 shard, 0 replicas, Basic features only):
  events:  sys-t-events-b<bucket>-g<gen>   one index per time bucket and generation
           write alias sys-t-events-w<bucket>  (bulk uses require_alias=true)
           read alias  sys-t-events            (all buckets)
  facts:   sys-t-facts-v<N>-g<gen>        one index per recipe version and generation
           write alias sys-t-facts-w<N>
           read alias  sys-t-facts          (the live recipe version)

The target index of a document is a pure function of immutable fields (bucket =
ts_event // BUCKET_DAYS), never "the current write index", so a replayed bulk
overwrites the same _id in the same index. A rollover-style change of generation
swaps the alias atomically and is only done under the tenant fence.
"""
import hashlib
import json
import os
import re
from datetime import timezone

from . import fusion
from .esc import ES, ESError, bulk_errors

BUCKET_DAYS = int(os.environ.get("MEMSYS_BUCKET_DAYS", "90"))

EVENTS_MAPPING = {"properties": {
    "event_id": {"type": "keyword"}, "tenant_id": {"type": "keyword"}, "session_id": {"type": "keyword"},
    "seq": {"type": "long"}, "ts_event": {"type": "date"}, "actor": {"type": "keyword"},
    "trust_origin": {"type": "keyword"}, "kind": {"type": "keyword"}, "subject_id": {"type": "keyword"},
    "content": {"type": "text"},
    "vec": {"type": "dense_vector", "dims": fusion.DIM, "similarity": "cosine", "index_options": {"type": "int8_hnsw"}}}}
FACTS_MAPPING = {"properties": {
    "id": {"type": "keyword"}, "tenant_id": {"type": "keyword"}, "subject": {"type": "keyword"},
    "predicate": {"type": "keyword"}, "scope": {"type": "keyword"}, "object": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
    "modality": {"type": "keyword"}, "status": {"type": "keyword"}, "author": {"type": "keyword"},
    "trust_origin": {"type": "keyword"}, "valid_from": {"type": "date"}, "valid_to": {"type": "date"},
    "input_event_ids": {"type": "keyword"}, "recipe_version": {"type": "integer"}, "subject_id": {"type": "keyword"},
    "cue": {"type": "text"}, "rev": {"type": "long"},
    "vec": {"type": "dense_vector", "dims": fusion.DIM, "similarity": "cosine", "index_options": {"type": "int8_hnsw"}}}}
SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0}


def install_templates(es: ES):
    for name, pat, mapping, prio in (("sys-emlab-events", "sys-*-events-b*", EVENTS_MAPPING, 300), ("sys-emlab-facts", "sys-*-facts-v*", FACTS_MAPPING, 301)):
        es.req("PUT", f"/_index_template/{name}", {"index_patterns": [pat], "priority": prio,
               "template": {"settings": SETTINGS, "mappings": mapping}})


def bucket_of(ts_event) -> int:
    return int(ts_event.astimezone(timezone.utc).timestamp() // 86400) // BUCKET_DAYS


def ev_index(t, b, g):  return f"sys-{t}-events-b{b:05d}-g{g}"
def ev_walias(t, b):    return f"sys-{t}-events-w{b:05d}"
def ev_ralias(t):       return f"sys-{t}-events"
def fx_index(t, v, g):  return f"sys-{t}-facts-v{v}-g{g}"
def fx_walias(t, v):    return f"sys-{t}-facts-w{v}"
def fx_ralias(t):       return f"sys-{t}-facts"


def _hash_doc(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def event_doc(tenant_id, session_id, seq, event_id, ts_iso, actor, trust_origin, kind, subject_id, content):
    return {"event_id": event_id, "tenant_id": tenant_id, "session_id": session_id, "seq": seq, "ts_event": ts_iso,
            "actor": actor, "trust_origin": trust_origin, "kind": kind, "subject_id": subject_id,
            "content": content, "vec": fusion.embed(content)}


def fact_doc(f: dict):
    d = {"id": f["id"], "tenant_id": f["tenant_id"], "subject": f["subject"], "predicate": f["predicate"],
         "scope": f["scope"], "object": f["object"], "modality": f["modality"], "status": f["status"],
         "author": f["author"], "trust_origin": f["trust_origin"], "valid_from": f["valid_from"],
         "valid_to": f["valid_to"], "input_event_ids": list(f["input_event_ids"]),
         "recipe_version": f["recipe_version"], "subject_id": f["subject_id"], "cue": f.get("cue"),
         "rev": f["rev"], "vec": fusion.embed(f"{f['subject']} {f['predicate']} {f['object']}")}
    return d


def doc_hash(doc: dict) -> str:
    """Content hash of a projected document, comparable between PG-built and ES-returned."""
    d = {k: v for k, v in doc.items() if k != "rev"}   # rev is a delivery version, not content
    if d.get("vec") is not None:
        d["vec"] = [int(round(x * 1e6)) for x in d["vec"]]   # float32 round-trip safe
    for k in ("valid_from", "valid_to", "ts_event"):
        if d.get(k):
            d[k] = norm_ts(d[k])
    return _hash_doc(d)


def norm_ts(s: str) -> str:
    """Normalise an ISO timestamp (as returned by ES or Postgres) to fixed-width UTC micros."""
    from datetime import datetime
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------- lifecycle
def alias_targets(es: ES, alias: str):
    s, j = es.req("GET", f"/_alias/{alias}", ok=(200, 404))
    return sorted(j.keys()) if s == 200 else []


def ensure_events_bucket(es: ES, t, b):
    w = ev_walias(t, b)
    if alias_targets(es, w):
        return
    name = ev_index(t, b, 1)
    try:
        es.req("PUT", f"/{name}", {"aliases": {w: {"is_write_index": True}, ev_ralias(t): {}}})
    except ESError as e:
        if "resource_already_exists" not in json.dumps(e.body):
            raise
        # index exists but the alias is missing: an interrupted generation swap; repair deterministically
        es.req("POST", "/_aliases", {"actions": [{"add": {"index": name, "alias": w, "is_write_index": True}},
                                               {"add": {"index": name, "alias": ev_ralias(t)}}]})


def ensure_facts_index(es: ES, t, v, live=False):
    w = fx_walias(t, v)
    if alias_targets(es, w):
        return
    name = fx_index(t, v, 1)
    aliases = {w: {"is_write_index": True}}
    if live:
        aliases[fx_ralias(t)] = {}
    try:
        es.req("PUT", f"/{name}", {"aliases": aliases})
    except ESError as e:
        if "resource_already_exists" not in json.dumps(e.body):
            raise
        es.req("POST", "/_aliases", {"actions": [{"add": {"index": name, "alias": a, **({"is_write_index": True} if a == w else {})}} for a in aliases]})


def gen_of(index_name: str) -> int:
    return int(re.search(r"-g(\d+)$", index_name).group(1))


def swap_generation(es: ES, old_index: str, new_index: str, aliases: dict):
    """Atomically move aliases to the new index and delete the old one in ONE request."""
    actions = [{"add": {"index": new_index, "alias": a, **opts}} for a, opts in aliases.items()]
    actions.append({"remove_index": {"index": old_index}})
    es.req("POST", "/_aliases", {"actions": actions})


def bulk_index(es: ES, lines, chunk=400):
    """Index doc lines [(action, source), ...] in chunks; raises on any non-tolerated item error."""
    for i in range(0, len(lines), chunk):
        flat = []
        for a, s in lines[i:i + chunk]:
            flat += [a, s]
        resp = es.bulk(flat, require_alias=True)
        bad = bulk_errors(resp)
        if bad:
            raise ESError(500, bad[:3], "bulk item errors")


def tenant_indices(es: ES, t):
    s, j = es.req("GET", f"/_cat/indices/sys-{t}-*", format="json", h="index,docs.count,uuid", ok=(200, 404))
    return j if s == 200 and isinstance(j, list) else []


def drop_tenant(es: ES, t):
    """Delete a tenant's indices by explicit name (wildcard deletes are disabled by default)."""
    names = [r["index"] for r in tenant_indices(es, t)]
    for i in range(0, len(names), 20):
        es.req("DELETE", "/" + ",".join(names[i:i + 20]), ok=(200, 404))
    return names
