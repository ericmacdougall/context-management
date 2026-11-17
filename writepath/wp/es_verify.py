"""Strict placeholder verification (delta review D4): content ABSENCE per tombstoned id, on every shard copy.

The check it replaces (Verifier.ext_counts at fd5d2a3) counted documents among the tombstoned ids that lack
erased:true. That passes four residues: a placeholder that still carries content next to erased:true, a missing
placeholder (no document is no count), a placeholder below version 2^62 (a later projector write with a higher
external version could overwrite it with content), and a shard copy the counted copy does not represent.

Here, for each family and each page of expected ids (events: every tombstoned event; facts: the derived id of every
tombstoned fact event, exactly the ids the cascade placed), on EVERY started copy of every index of the tenant's family
(each copy searched alone: preference _shards:<n>|_only_nodes:<node id>):
  es:placeholder_content   a hit whose _source is anything but {"erased": true}
  es:placeholder_version   a hit whose _version is not PLACEHOLDER_VERSION (2^62)
  es:placeholder_missing   an expected id absent from a copy of the family's base index (the erase alias target)
  es:copy_unverified       a copy of a family index that is assigned to a node but not STARTED (it cannot be
                           searched alone, so it is not shown clean; the job stays open for the next sweep), or whose
                           search did not complete (timed out, terminated early, a failed, skipped or unsearched shard:
                           review C F9); a base copy counted here does not show any id placed
Every count must be 0 for wp_erasure_complete to accept the job (it rejects any non-zero ext entry).

A copy is counted once per (copy, id): a residue on two copies counts twice, so the receipt shows how many copies are
affected. What this does NOT see: segments of a copy (soft-deleted bytes: es:soft_deleted and expunge), snapshots and
async-search results (registry ext:*)."""
from . import families as F

PLACEHOLDER_SOURCE = {"erased": True}
KEYS = ("es:placeholder_content", "es:placeholder_version", "es:placeholder_missing", "es:copy_unverified")


def copies(es, pattern):
    """[(index, shard, prirep, state, node_id)] for every shard copy of the indices matching pattern."""
    s, j = es.req("GET", f"/_cat/shards/{pattern}", ok=(200, 404), format="json", h="index,shard,prirep,state,id",
                  expand_wildcards="all")
    if s != 200:
        return []
    return [(r["index"], str(r["shard"]), r["prirep"], r["state"], r.get("id")) for r in j]


def _complete(j):
    """True only for a response that searched exactly the one requested shard copy to the end (review C F9): not timed
    out, not terminated early, and _shards total = successful = 1 with nothing failed or skipped. Anything else (or
    missing metadata) is not evidence of absence."""
    sh = j.get("_shards") if isinstance(j, dict) else None
    return (isinstance(sh, dict) and j.get("timed_out") is False and j.get("terminated_early") is not True
            and sh.get("total") == 1 and sh.get("successful") == 1 and sh.get("failed") == 0 and sh.get("skipped", 0) == 0)


def _search_copy(es, index, shard, node, ids):
    """The copy's hits, or None when the search did not complete (allow_partial_search_results=false is sent too)."""
    s, j = es.req("POST", f"/{index}/_search", {"query": {"ids": {"values": list(ids)}}, "size": len(ids), "version": True,
                                                "_source": True, "track_total_hits": False},
                  ok=(200,), preference=f"_shards:{shard}|_only_nodes:{node}", allow_partial_search_results="false")
    return j["hits"]["hits"] if _complete(j) else None


def check_page(es, t, f, ids, cps=None, detail=None):
    """Counts for one family and one page of expected ids, over every copy. detail (a dict of lists) collects up to
    20 offending ids per key; ids are opaque hashes, never content."""
    out = dict.fromkeys(KEYS, 0)
    if not ids:
        return out
    base = F.base_index(t, f)
    cps = copies(es, f"{F.P}{t}-{f}-*") if cps is None else cps
    want = set(ids)
    seen_base_copy = False

    def note(k, i):
        out[k] += 1
        if detail is not None and len(detail.setdefault(k, [])) < 20:
            detail[k].append(i)

    for index, shard, prirep, state, node in cps:
        if state != "STARTED":
            if node:
                note("es:copy_unverified", f"{index}[{shard}]{prirep}:{state}")
            continue
        hits = _search_copy(es, index, shard, node, ids)
        if hits is None:                       # an incomplete search proves nothing about this copy (F9)
            note("es:copy_unverified", f"{index}[{shard}]{prirep}:incomplete")
            continue
        got = set()
        for h in hits:
            got.add(h["_id"])
            if h.get("_source") != PLACEHOLDER_SOURCE:
                note("es:placeholder_content", h["_id"])
            if h.get("_version") != F.PLACEHOLDER_VERSION:
                note("es:placeholder_version", h["_id"])
        if index == base:
            seen_base_copy = True
            for i in sorted(want - got):
                note("es:placeholder_missing", i)
    if not seen_base_copy:                     # no started copy of the base index: nothing proves any id is placed
        for i in sorted(want):
            note("es:placeholder_missing", i)
    return out


def check(es, t, pages, derive_fact_id, detail=None):
    """pages: iterable of lists of (event_id, kind) (erasure.tombstoned_ids). Returns summed counts over both families."""
    out = dict.fromkeys(KEYS, 0)
    cps = {f: None for f in ("events", "facts")}
    for page in pages:
        for f, ids in (("events", [e for e, _ in page]), ("facts", [derive_fact_id(t, e) for e, k in page if k == "fact"])):
            if not ids:
                continue
            if cps[f] is None:
                cps[f] = copies(es, f"{F.P}{t}-{f}-*")
            for k, v in check_page(es, t, f, ids, cps[f], detail).items():
                out[k] += v
    return out
