"""Decision core on Postgres: the same pure reducer (pol.core.apply), made concurrency-safe by lock order, not by new rules.

Tables (database `pol` of the experiment's own container):
  dc_authority  one row: principals, authority map, admins, revoked   (decisions take FOR SHARE, revoke/grant take FOR UPDATE)
  dc_reqs       one row per requirement: the reducer's per-requirement state  (a head-moving event takes FOR UPDATE)
  dc_events     the log: seq, unique idem key, event, outcome code
Lock order is always authority then requirements by name, so no deadlock. After the locks are held the idempotency row is
read, so an event whose twin committed while it waited returns `duplicate` instead of applying twice (the unique index is the
backstop). READ COMMITTED is enough because every decision serialises on its requirement row and re-reads the head under the lock.
"""
from __future__ import annotations

import json
import os

import psycopg

from .core import REFUSED, apply, canon, genesis, idem_key, manifest_hash, sha

PG = dict(host="127.0.0.1", port=int(os.environ.get("POL_PG_PORT", "25470")), dbname="pol", user="postgres")

DDL = """
drop table if exists dc_events, dc_reqs, dc_authority, dc_lww cascade;
create table dc_authority(tenant text primary key, doc jsonb not null);
create table dc_reqs(tenant text not null, req text not null, doc jsonb not null, primary key (tenant, req));
create table dc_events(seq bigserial primary key, tenant text not null, idem text, ev jsonb not null, code text not null, fp text not null, req text, evid text);
create unique index dc_events_idem on dc_events(tenant, idem) where idem is not null;          -- backstop for concurrent twins on different requirements
create unique index dc_events_evid on dc_events(tenant, req, evid) where evid is not null;     -- event ids unique per requirement (applied events)
create table dc_lww(req text primary key, value text, decided_by text, version int not null default 0);
"""

AUTH_KEYS = ("tenant", "principals", "authority", "admins", "revoked", "req_labels")
BACKSTOP_RETRIES = [0]          # witness: how often the unique index (not a row lock) decided a race
RETRYABLE = (psycopg.errors.UniqueViolation, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)


def connect(autocommit=True, app="pol"):
    c = psycopg.connect(autocommit=autocommit, application_name=app, **PG)
    c.isolation_level = psycopg.IsolationLevel.READ_COMMITTED       # explicit: the lock protocol is argued for READ COMMITTED
    return c


def setup(conn, tenants):
    """tenants: {tenant: (principals, authority, admins)}; one authority row and one state per tenant."""
    with conn.transaction():
        conn.execute(DDL)
        out = {}
        for t, (principals, authority, admins) in tenants.items():
            g = genesis(t, principals, authority, admins)
            conn.execute("insert into dc_authority values (%s, %s)", (t, json.dumps({k: g[k] for k in AUTH_KEYS})))
            for r, q in g["reqs"].items():
                conn.execute("insert into dc_reqs values (%s, %s, %s)", (t, r, json.dumps(q)))
                conn.execute("insert into dc_lww(req) values (%s) on conflict do nothing", (r,))
            out[t] = g
    return out


def _load_state(conn, ev):
    t = ev.get("tenant")
    t_ = t if isinstance(t, str) else ""
    kind = ev.get("type")
    lock = "for update" if kind in ("revoke", "grant") else "for share"
    row = conn.execute("select doc from dc_authority where tenant=%s " + lock, (t_,)).fetchone()
    if row is None:
        return None
    st = dict(row[0])
    if kind in ("revoke", "grant"):
        rows = conn.execute("select req, doc from dc_reqs where tenant=%s order by req for update", (t_,)).fetchall()
    else:
        r = ev.get("req")
        rows = conn.execute("select req, doc from dc_reqs where tenant=%s and req=%s for update", (t_, r)).fetchall() if isinstance(r, str) else []
    st["reqs"] = {r: q for r, q in rows}
    return st


def submit(conn, ev, hook=None, flags=frozenset()):
    """Apply one event atomically. hook(name, conn) is called inside the transaction: 'locked' (all locks held, before apply) and
    'before_commit'. Returns the outcome code. Unique-index races and serialisation failures are retried (the retry re-reads under the locks)."""
    for attempt in range(6):
        try:
            with conn.transaction():
                st = _load_state(conn, ev)
                if hook:
                    hook("locked", conn)
                if st is None:
                    return "tenant_mismatch"
                t = ev["tenant"]
                ik = idem_key(ev) if isinstance(ev.get("idem"), str) and isinstance(ev.get("principal"), str) else None
                row = conn.execute("select code, fp from dc_events where tenant=%s and idem=%s", (t, ik)).fetchone() if ik else None
                st["idem"] = {ik: [row[1], row[0]]} if row else {}
                before_reqs = {r: canon(q) for r, q in st["reqs"].items()}
                before_auth = canon({k: st[k] for k in AUTH_KEYS})
                new, code = apply(st, ev, flags)
                if code not in ("duplicate", "idem_conflict", "bad_event", "tenant_mismatch"):
                    for r, q in new["reqs"].items():
                        if canon(q) != before_reqs.get(r):
                            conn.execute("update dc_reqs set doc=%s where tenant=%s and req=%s", (json.dumps(q), t, r))
                    if canon({k: new[k] for k in AUTH_KEYS}) != before_auth:
                        conn.execute("update dc_authority set doc=%s where tenant=%s", (json.dumps({k: new[k] for k in AUTH_KEYS}), t))
                    applied = code not in REFUSED
                    conn.execute("insert into dc_events(tenant, idem, ev, code, fp, req, evid) values (%s,%s,%s,%s,%s,%s,%s)",
                                 (t, ik if applied else None, json.dumps(ev), code, sha(ev), ev.get("req") if isinstance(ev.get("req"), str) else None,
                                  ev["id"] if applied and isinstance(ev.get("req"), str) else None))
                if hook:
                    hook("before_commit", conn)
            return code
        except RETRYABLE as e:
            if isinstance(e, psycopg.errors.UniqueViolation):
                BACKSTOP_RETRIES[0] += 1
            if attempt == 5:
                raise
    raise RuntimeError("unreachable")


def read_state(conn, tenant):
    a = conn.execute("select doc from dc_authority where tenant=%s", (tenant,)).fetchone()[0]
    reqs = {r: q for r, q in conn.execute("select req, doc from dc_reqs where tenant=%s order by req", (tenant,)).fetchall()}
    return dict(a, reqs=reqs)


def log_rows(conn, tenant):
    return [ev for (ev,) in conn.execute("select ev from dc_events where tenant=%s order by seq", (tenant,)).fetchall()]


def rebuild(conn, tenant, principals, authority, admins):
    st = genesis(tenant, principals, authority, admins)
    for ev in log_rows(conn, tenant):
        st, _c = apply(st, ev)
    return st


# ------------------------------------------------------------------------------------------------ controls (broken variants)
def submit_blind_lww(conn, req, value, who, hook=None):
    """CONTROL: no lock, no version check, no contested state: the later UPDATE silently wins."""
    with conn.transaction():
        conn.execute("update dc_lww set value=%s, decided_by=%s, version=version+1 where req=%s", (value, who, req))
        if hook:
            hook("locked", conn)
            hook("before_commit", conn)


def submit_nolock(conn, ev, hook=None):
    """CONTROL: the reducer on a snapshot read WITHOUT a lock; write back unconditionally (classic lost update)."""
    with conn.transaction():
        a = conn.execute("select doc from dc_authority where tenant=%s", (ev["tenant"],)).fetchone()[0]
        q = conn.execute("select doc from dc_reqs where tenant=%s and req=%s", (ev["tenant"], ev["req"])).fetchone()[0]
        st = dict(a, reqs={ev["req"]: q}, idem={})
        if hook:
            hook("after_read", conn)
        new, code = apply(st, ev)
        conn.execute("update dc_reqs set doc=%s where tenant=%s and req=%s", (json.dumps(new["reqs"][ev["req"]]), ev["tenant"], ev["req"]))
        conn.execute("insert into dc_events(tenant, idem, ev, code, fp, req) values (%s,%s,%s,%s,%s,%s)", (ev["tenant"], idem_key(ev), json.dumps(ev), code, sha(ev), ev["req"]))
    return code
