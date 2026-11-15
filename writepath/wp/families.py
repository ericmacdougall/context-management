"""Elasticsearch projection families and the epoch-bound write aliases that fence them.

Per tenant t and registered family f (FAMILIES is the single list; registry.py and the erasure iterate it):
  base index      wp16096-<t>-<f>-base             first index, fixed name, so concurrent creation is create-once
  rebuilt index   wp16096-<t>-<f>-e<E>-<nonce>     made by a rebuild under projector epoch E
  read alias      wp16096-<t>-<f>
  write alias     wp16096-<t>-<f>-w<E>             exists ONLY for the current projector epoch E
  staging alias   wp16096-<t>-<f>-s<E>             attached to a rebuild index only under a successful CAS

Every document write uses require_alias=true (es.bulk_fenced), so it can only land through w<E> or s<E>.
fence_es(E) is an alias compare-and-set: it observes the family's write aliases, refuses if any is newer than E, and
in ONE atomic _aliases request removes each observed alias with must_exist=true and adds w<E>. If anything changed
since the observation the request fails as a whole (verified live: exp/es_semantics.py P3). Hence once a holder of
epoch E' > E has fenced, no write by a holder of E can land in any index the readers see.

Docs: https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-update-aliases and
https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk (read 2026-11-15; behaviour confirmed by
the live probe on 9.5.6 Basic, not only by the docs: the docs say partial per-action results are possible)."""
import re
import uuid

from . import config
from .es import ES, ESError

P = config.INDEX_PREFIX
SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0}

FAMILIES = {
    "events": {"mappings": {"properties": {
        "event_id": {"type": "keyword"}, "session_id": {"type": "keyword"}, "seq": {"type": "long"}, "kind": {"type": "keyword"},
        "subject_id": {"type": "keyword"}, "actor": {"type": "keyword"}, "trust_origin": {"type": "keyword"},
        "ts_event": {"type": "date"}, "content": {"type": "text"}}}},
    "facts": {"mappings": {"properties": {
        "fact_id": {"type": "keyword"}, "subject_id": {"type": "keyword"}, "entity": {"type": "keyword"}, "attribute": {"type": "keyword"},
        "value": {"type": "text", "fields": {"raw": {"type": "keyword"}}}, "valid_from": {"type": "date"}, "valid_to": {"type": "date"},
        "input_event_ids": {"type": "keyword"}, "rev": {"type": "long"}}}},
}


def read_alias(t, f):       return f"{P}{t}-{f}"
def write_alias(t, f, e):   return f"{P}{t}-{f}-w{e}"
def stage_alias(t, f, e):   return f"{P}{t}-{f}-s{e}"
def base_index(t, f):       return f"{P}{t}-{f}-base"
def rebuilt_index(t, f, e): return f"{P}{t}-{f}-e{e}-{uuid.uuid4().hex[:8]}"


def family_index_re(t, f):
    return re.compile(rf"^{re.escape(P + t)}-{re.escape(f)}-(base|e\d+-[0-9a-f]{{8}})$")


class StaleEpoch(Exception):
    pass


def _family_state(es: ES, t, f):
    """{index: aliases} for the family's indices."""
    rx = family_index_re(t, f)
    return {i: a for i, a in es.alias_map(f"{P}{t}-{f}-*").items() if rx.match(i)}


def _walias_epochs(t, f, aliases):
    """[(alias, epoch)] for the write (w) and staging (s) aliases among `aliases`."""
    rx = re.compile(rf"^{re.escape(P + t)}-{re.escape(f)}-[ws](\d+)$")
    return [(a, int(m.group(1))) for a in aliases for m in [rx.match(a)] if m]


def ensure_family(es: ES, t, f):
    """Create the base index once (fixed name: a second creator gets resource_already_exists and moves on)."""
    if _family_state(es, t, f):
        return
    try:
        es.req("PUT", f"/{base_index(t, f)}", {"settings": SETTINGS, "mappings": FAMILIES[f]["mappings"], "aliases": {read_alias(t, f): {}}})
    except ESError as e:
        if e.etype != "resource_already_exists_exception":
            raise


def fence_es(es: ES, t, epoch):
    """Alias CAS for ALL registered families in one atomic request. Raises StaleEpoch if a newer epoch is visible or
    if the observed state changed before the request applied."""
    actions = []
    for f in FAMILIES:
        ensure_family(es, t, f)
        st = _family_state(es, t, f)
        live = [i for i, al in st.items() if read_alias(t, f) in al]
        if len(live) != 1:
            raise RuntimeError(f"family {f}: read alias on {len(live)} indices")
        seen = [(i, a, e) for i, al in st.items() for a, e in _walias_epochs(t, f, al)]
        if any(e > epoch for _, _, e in seen):
            raise StaleEpoch(f"{f}: newer write/staging alias than e{epoch}")
        if any(a == write_alias(t, f, epoch) for _, a, _ in seen):
            continue                                     # already fenced at this epoch
        for i, a, e in seen:                             # older write AND staging aliases go, each as a CAS
            actions.append({"remove": {"index": i, "alias": a, "must_exist": True}})
        actions.append({"add": {"index": live[0], "alias": write_alias(t, f, epoch), "is_write_index": True}})
    if not actions:
        return
    try:
        es.aliases(actions)
    except ESError as e:
        raise StaleEpoch(f"alias CAS failed: {e.etype}") from None


def assert_epoch(es: ES, t, f, epoch, extra_actions=()):
    """CAS that w<epoch> still exists (remove+re-add it in one request) together with extra_actions."""
    st = _family_state(es, t, f)
    live = [i for i, al in st.items() if read_alias(t, f) in al]
    acts = [{"remove": {"index": live[0], "alias": write_alias(t, f, epoch), "must_exist": True}},
            {"add": {"index": live[0], "alias": write_alias(t, f, epoch), "is_write_index": True}}] + list(extra_actions)
    try:
        es.aliases(acts)
    except ESError as e:
        raise StaleEpoch(f"epoch CAS failed: {e.etype}") from None
    return live[0], st


def tenant_indices(es: ES, t):
    return es.indices(f"{P}{t}-*")
