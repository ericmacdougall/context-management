"""Elasticsearch projection families, the epoch-bound write aliases that fence the projector, and the per-document
erasure fence (placeholders).

Per tenant t and registered family f (FAMILIES is the single list; registry.py and the erasure iterate it):
  index           wp16096-<t>-<f>-base             one index, fixed name, so concurrent creation is create-once
  read alias      wp16096-<t>-<f>                  is_write_index=false: nothing writes through it
  write alias     wp16096-<t>-<f>-w<E>             exists ONLY for the current projector epoch E
  erase alias     wp16096-<t>-<f>-x                placeholders only (erasure)

Projector fence: every document write uses require_alias=true (es.bulk_fenced), so it can only land through an
alias. fence_es(E) is an alias compare-and-set: it observes the family's write aliases, refuses if any is newer than
E, and in ONE atomic _aliases request removes each observed alias with must_exist=true and adds w<E> (probe P3: the
request fails as a whole if anything changed; es.aliases also raises on errors:true).

Erasure fence (per document, no time bound, no projector lease): for every erased event id (from Postgres
tombstones, never from what ES happens to contain) a content-free placeholder is written at the same _id with
external version 2^62. Event docs are written with op_type=create and fact docs with external_gte versions far
below 2^62, so a stale projector write that arrives later gets 409 in either order (probes P9-P11), and a write that
arrived earlier is overwritten. The overwritten bytes stay in soft-deleted segments until the peer-recovery
retention lease advances; expunge() waits for that and then force-merges with only_expunge_deletes (probe P12).

Docs (read 2026-11-15/16): https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-update-aliases ,
https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk ,
https://www.elastic.co/docs/reference/elasticsearch/index-settings/merge (expunge_deletes_allowed) ,
https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-forcemerge (only_expunge_deletes).
Behaviour is taken from the live probes on 9.5.6 Basic (exp/es_semantics.py), not only from the docs."""
import re
import time

from . import config
from .es import ES, ESError

P = config.INDEX_PREFIX
SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0, "index.merge.policy.expunge_deletes_allowed": 0}
PLACEHOLDER_VERSION = 2 ** 62

FAMILIES = {
    "events": {"mappings": {"properties": {
        "event_id": {"type": "keyword"}, "session_id": {"type": "keyword"}, "seq": {"type": "long"}, "kind": {"type": "keyword"},
        "subject_id": {"type": "keyword"}, "actor": {"type": "keyword"}, "trust_origin": {"type": "keyword"},
        "mentions": {"type": "keyword"},
        "ts_event": {"type": "date"}, "content": {"type": "text"}, "erased": {"type": "boolean"}}}},
    "facts": {"mappings": {"properties": {
        "fact_id": {"type": "keyword"}, "subject_id": {"type": "keyword"}, "entity": {"type": "keyword"}, "attribute": {"type": "keyword"},
        "value": {"type": "text", "fields": {"raw": {"type": "keyword"}}}, "valid_from": {"type": "date"}, "valid_to": {"type": "date"},
        "input_event_ids": {"type": "keyword"}, "rev": {"type": "long"}, "erased": {"type": "boolean"}}}},
}


def read_alias(t, f):       return f"{P}{t}-{f}"
def write_alias(t, f, e):   return f"{P}{t}-{f}-w{e}"
def erase_alias(t, f):      return f"{P}{t}-{f}-x"
def base_index(t, f):       return f"{P}{t}-{f}-base"


def family_index_re(t, f):
    return re.compile(rf"^{re.escape(P + t)}-{re.escape(f)}-base$")


class StaleEpoch(Exception):
    """A CONFIRMED loss of the projector epoch: a newer write alias is visible (or the lease no longer renews)."""


class ProjectionError(Exception):
    """An Elasticsearch failure that is NOT a confirmed loss of the epoch (review-2 N1): it must propagate, never look
    like a lost race or like idle success. Carries the error type and status only (never a response body)."""


CAS_ATTEMPTS = 3


def newer_epoch_visible(es: ES, t, f, epoch):
    """Re-read one family's write aliases (a failure here propagates: it is itself an infrastructure error)."""
    return any(e > epoch for als in _family_state(es, t, f).values() for _, e in _walias_epochs(t, f, als))


def _family_state(es: ES, t, f):
    """{index: aliases} for the family's indices."""
    rx = family_index_re(t, f)
    return {i: a for i, a in es.alias_map(f"{P}{t}-{f}-*").items() if rx.match(i)}


def _walias_epochs(t, f, aliases):
    rx = re.compile(rf"^{re.escape(P + t)}-{re.escape(f)}-w(\d+)$")
    return [(a, int(m.group(1))) for a in aliases for m in [rx.match(a)] if m]


def ensure_family(es: ES, t, f):
    """Create the index once (fixed name: a second creator gets resource_already_exists and moves on)."""
    if _family_state(es, t, f):
        return
    try:
        es.req("PUT", f"/{base_index(t, f)}", {"settings": SETTINGS, "mappings": FAMILIES[f]["mappings"],
                                                "aliases": {read_alias(t, f): {"is_write_index": False}, erase_alias(t, f): {"is_write_index": True}}})
    except ESError as e:
        if e.etype != "resource_already_exists_exception":
            raise


def _observe(es: ES, t, epoch):
    """({family: (index, aliases)}, actions) for the CAS; raises StaleEpoch if a newer epoch is already visible."""
    seen_all, actions = {}, []
    for f in FAMILIES:
        ensure_family(es, t, f)
        st = _family_state(es, t, f)
        if len(st) != 1:
            raise RuntimeError(f"family {f}: {len(st)} indices")
        (idx, als), = st.items()
        seen_all[f] = (idx, frozenset(als))
        seen = _walias_epochs(t, f, als)
        if any(e > epoch for _, e in seen):
            raise StaleEpoch(f"{f}: newer write alias than e{epoch}")
        if any(e == epoch for _, e in seen):
            continue
        for a, _ in seen:
            actions.append({"remove": {"index": idx, "alias": a, "must_exist": True}})
        actions.append({"add": {"index": idx, "alias": write_alias(t, f, epoch), "is_write_index": True}})
    return seen_all, actions


def fence_es(es: ES, t, epoch):
    """Alias CAS for ALL registered families in one atomic request.

    A failed request is classified by re-reading the aliases (review-2 N1; at 1fcf3f7 every ESError became StaleEpoch):
      a newer write alias is visible        -> StaleEpoch (a confirmed loss: another projector holds a newer epoch)
      the aliases changed, nothing newer    -> observe again and retry the CAS (at most CAS_ATTEMPTS requests)
      the aliases are unchanged             -> ProjectionError (the request failed for another reason)
    A failure of the re-read itself propagates as it is (an infrastructure error)."""
    for _ in range(CAS_ATTEMPTS):
        seen_all, actions = _observe(es, t, epoch)
        if not actions:
            return
        try:
            es.aliases(actions)
            return
        except ESError as e:
            err = e
        now = {f: (idx, frozenset(als)) for f in FAMILIES for idx, als in _family_state(es, t, f).items()}
        if any(e > epoch for f, (_, als) in now.items() for _, e in _walias_epochs(t, f, als)):
            raise StaleEpoch(f"alias CAS lost to a newer epoch ({err.etype})") from None
        if now == seen_all:
            raise ProjectionError(f"alias CAS failed: ES {err.status} {err.etype}") from None
    raise ProjectionError(f"alias CAS: the aliases changed under {CAS_ATTEMPTS} attempts with no newer epoch") from None


def place_placeholders(es: ES, t, f, ids, batch=1000):
    """Content-free placeholders at the given _ids (version 2^62). Returns (written, already_placeholders)."""
    ensure_family(es, t, f)
    w = same = 0
    for i in range(0, len(ids), batch):
        acts = [({"index": {"_index": erase_alias(t, f), "_id": d, "version": PLACEHOLDER_VERSION, "version_type": "external"}}, {"erased": True})
                for d in ids[i:i + batch]]
        ok, bad = es.bulk_fenced(acts)
        hard = [b for b in bad if b[1] != 409]            # 409: a placeholder is already there (idempotent rerun)
        if hard:
            raise ESError(hard[0][1], hard[0][2], f"placeholders {f}")
        w += ok
        same += len(bad)
    return w, same


def expunge(es: ES, t, wait_s=180.0, poll_s=2.0):
    """Remove overwritten documents' bytes from the tenant's family indices: refresh, flush, wait until every
    retention lease retains nothing at or below the current max_seq_no, then _forcemerge?only_expunge_deletes and
    flush. Returns {index: {'lease_wait_s', 'docs_deleted_after'}}; raises TimeoutError if the leases do not advance."""
    return {base_index(t, f): expunge_index(es, base_index(t, f), wait_s, poll_s) for f in FAMILIES}


def expunge_index(es: ES, idx, wait_s=180.0, poll_s=2.0):
    """One index: refresh, flush, wait for every retention lease to pass max_seq_no, expunge deletes, flush."""
    es.req("POST", f"/{idx}/_refresh")
    es.req("POST", f"/{idx}/_flush")
    j = es.req("GET", f"/{idx}/_stats", level="shards")[1]
    target = max(sh["seq_no"]["max_seq_no"] for copies in j["indices"][idx]["shards"].values() for sh in copies)
    t0 = time.time()
    while True:
        j = es.req("GET", f"/{idx}/_stats", level="shards")[1]
        leases = [l["retaining_seq_no"] for copies in j["indices"][idx]["shards"].values() for sh in copies for l in sh["retention_leases"]["leases"]]
        if all(r > target for r in leases):
            break
        if time.time() - t0 > wait_s:
            raise TimeoutError(f"retention leases did not pass seq {target} on {idx}")
        time.sleep(poll_s)
    waited = round(time.time() - t0, 1)
    es.req("POST", f"/{idx}/_forcemerge", only_expunge_deletes=True)
    es.req("POST", f"/{idx}/_flush")
    st = es.req("GET", f"/{idx}/_stats/docs")[1]["_all"]["primaries"]["docs"]
    return {"lease_wait_s": waited, "docs_deleted_after": st["deleted"], "target_seq": target}


def tenant_indices(es: ES, t):
    return es.indices(f"{P}{t}-*")
