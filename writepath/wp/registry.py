"""Store inventory as code: every place content can live, with its content class, erase strategy and verifier.

The erasure cascade, the completion check (mirrored into Postgres table store_registry) and tombstone filtering all
iterate STORES. `unregistered()` introspects the real system (both Postgres databases, every Elasticsearch index,
alias and template under the prefix, the log directory) and returns anything not covered: a deploy or test with a
non-empty result fails (exp/registry_check.py)."""
import re
from dataclasses import dataclass, field

from . import config, families as F

P = config.INDEX_PREFIX


@dataclass
class Store:
    name: str
    kind: str                 # pg | keys | es | dir | external
    where: str                # table, family or path
    content: str              # what it holds
    erase: str                # how erasure reaches it
    verifiable: bool          # True: a verifier counts residue for a subject and 'done' requires 0
    note: str = ""
    extra: dict = field(default_factory=dict)


def _pg(table, content, erase, verifiable, note=""):
    return Store(f"pg:{table}", "pg", table, content, erase, verifiable, note)


STORES = [
    _pg("events", "ciphertext under the subject key (AAD-bound)", "tombstone in phase 1; key destroyed (crypto-shred)", True),
    _pg("quarantine", "ciphertext of unadopted stale writes", "rows of the subject deleted in phase 1; key destroyed", True),
    _pg("inbox", "ciphertext payloads", "rows of the subject deleted in phase 1", True),
    _pg("analyst_jobs", "ciphertext results; input ids", "jobs over the subject's events deleted in phase 1; completion refused over erased inputs", True),
    _pg("facts", "ciphertext fact bodies; keyed slot hash", "facts derived from the subject's events deleted in phase 1", True),
    _pg("tombstones", "event ids only", "kept (needed to filter readers and rebuilds)", False),
    _pg("subjects", "subject ids and erased flag", "kept as the erasure fence", False),
    _pg("chains", "session head hash", "kept (hash only)", False),
    _pg("leases", "lease epochs", "no content", False),
    _pg("cursors", "projection cursors", "no content", False),
    _pg("decision_heads", "keyed item hash, version, event id", "no plaintext; the item hash uses the tenant key (survives erasure, see N15)", False),
    _pg("store_registry", "this list", "no content", False),
    _pg("erasure_jobs", "subject id, event ids, verify counts, error TYPE only", "kept as the audit record", False),
    Store("keys:subject_keys", "keys", "subject_keys", "per-subject data keys", "deleted + VACUUM + WAL switch/checkpoint", True),
    Store("keys:destroyed", "keys", "destroyed", "subject ids", "kept: refuses re-creating an erased subject's key", False),
    Store("keys:tenant_keys", "keys", "tenant_keys", "tenant HMAC key", "kept; HMACs of erased subjects stay confirmable with it (N15, open)", False),
    Store("es:events", "es", "events", "plaintext event content", "every index of the family rebuilt from surviving rows under a new epoch; old indices deleted", True),
    Store("es:facts", "es", "facts", "plaintext fact values", "as es:events", True),
    Store("dir:logs", "dir", "logs", "no content by construction (errors keep type and status only)", "marker scan in exp/erasure_e2e.py", False,
          extra={"allowed": [r".*\.log$"]}),
    # Stores that exist in a client deployment but are NOT exercised here: listed so nothing is silently assumed covered.
    Store("ext:es-snapshots", "external", "snapshot repositories", "plaintext segment copies", "UNTESTED: restore must be gated on replaying tombstones", False),
    Store("ext:es-async-search", "external", ".async-search", "stored search responses", "UNTESTED (expire with keep_alive; not used by this code)", False),
    Store("ext:kibana-agent-builder", "external", "Kibana saved objects / conversations", "conversation text", "UNTESTED", False),
    Store("ext:es-slowlog", "external", "ES slow logs", "query text", "UNTESTED (slowlog off by default)", False),
    Store("ext:pg-wal-backups", "external", "Postgres WAL archive, replicas, backups", "ciphertext and keys", "UNTESTED: needs a KMS with real deletion and stated backup retention", False),
    Store("ext:process-memory", "external", "process heaps", "transient plaintext", "UNTESTED", False),
]

BY_NAME = {s.name: s for s in STORES}
for _f in F.FAMILIES:
    assert f"es:{_f}" in BY_NAME, f"family {_f} not registered"


def verifiable():
    return [s for s in STORES if s.verifiable]


def sync_db(conn):
    with conn.transaction():
        conn.execute("delete from store_registry")
        for s in STORES:
            conn.execute("insert into store_registry(name, verifiable) values (%s,%s)", (s.name, s.verifiable))


# --------------------------------------------------------------- introspection
def _pg_relations(conn):
    return {r[0] for r in conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p','v','m','f') "
        "and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%%'").fetchall()}


def unregistered(conn, kconn, es):
    """Everything that can hold content and is not in STORES. Empty list = inventory complete."""
    out = []
    pg = {s.where for s in STORES if s.kind == "pg"}
    out += [f"pg:{r}" for r in sorted(_pg_relations(conn) - pg)]
    kk = {s.where for s in STORES if s.kind == "keys"}
    out += [f"keys:{r}" for r in sorted(_pg_relations(kconn) - kk)]
    fam_rx = re.compile(rf"^{re.escape(P)}[a-z0-9_]+-({'|'.join(map(re.escape, F.FAMILIES))})-(base|e\d+-[0-9a-f]{{8}})$")
    out += [f"es-index:{i}" for i in sorted(es.indices(P + "*")) if not fam_rx.match(i)]
    alias_rx = re.compile(rf"^{re.escape(P)}[a-z0-9_]+-({'|'.join(map(re.escape, F.FAMILIES))})(-[ws]\d+)?$")
    for i, als in es.alias_map(P + "*").items():
        out += [f"es-alias:{a}" for a in sorted(als) if not alias_rx.match(a)]
    s, j = es.req("GET", f"/_index_template/{P}*", ok=(200, 404))
    out += [f"es-template:{t['name']}" for t in (j.get("index_templates", []) if s == 200 else [])]
    s, j = es.req("GET", f"/_component_template/{P}*", ok=(200, 404))
    out += [f"es-component:{t['name']}" for t in (j.get("component_templates", []) if s == 200 else [])]
    logs = BY_NAME["dir:logs"]
    if config.LOG_DIR.exists():
        for p in config.LOG_DIR.rglob("*"):
            if p.is_file() and not any(re.match(rx, p.name) for rx in logs.extra["allowed"]):
                out.append(f"dir:{p}")
    return out
