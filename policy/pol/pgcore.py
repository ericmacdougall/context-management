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

from .core import apply, canon, genesis, manifest_hash, sha

PG = dict(host="127.0.0.1", port=int(os.environ.get("POL_PG_PORT", "25470")), dbname="pol", user="postgres")

DDL = """
drop table if exists dc_events, dc_reqs, dc_authority, dc_lww cascade;
create table dc_authority(id int primary key, doc jsonb not null);
create table dc_reqs(req text primary key, doc jsonb not null);
create table dc_events(seq bigserial primary key, idem text unique not null, ev jsonb not null, code text not null, fp text not null, req text);
create table dc_lww(req text primary key, value text, decided_by text, version int not null default 0);
"""


def connect(autocommit=True, app="pol"):
    return psycopg.connect(autocommit=autocommit, application_name=app, **PG)


def setup(conn, principals, authority, admins):
    with conn.transaction():
        conn.execute(DDL)
        g = genesis(principals, authority, admins)
        conn.execute("insert into dc_authority values (1, %s)", (json.dumps({k: g[k] for k in ("principals", "authority", "admins", "revoked")}),))
        for r, q in g["reqs"].items():
            conn.execute("insert into dc_reqs values (%s, %s)", (r, json.dumps(q)))
            conn.execute("insert into dc_lww(req) values (%s)", (r,))
    return g


def _load_state(conn, ev, locks):
    t = ev.get("type")
    if t in ("revoke", "grant"):
        a = conn.execute("select doc from dc_authority where id=1 for update").fetchone()[0]
        rows = conn.execute("select req, doc from dc_reqs order by req for update").fetchall()
    else:
        a = conn.execute("select doc from dc_authority where id=1 for share").fetchone()[0]
        r = ev.get("req")
        rows = conn.execute("select req, doc from dc_reqs where req=%s for update", (r,)).fetchall() if isinstance(r, str) else []
    st = dict(a)
    st["reqs"] = {r: q for r, q in rows}
    return st


def submit(conn, ev, hook=None):
    """Apply one event atomically. hook(name, conn) is called inside the transaction: 'locked' (all locks held, before apply) and
    'before_commit'. Returns the outcome code."""
    for attempt in range(3):
        try:
            with conn.transaction():
                st = _load_state(conn, ev, None)
                if hook:
                    hook("locked", conn)
                row = conn.execute("select ev, code, fp from dc_events where idem=%s", (ev.get("idem"),)).fetchone() if isinstance(ev.get("idem"), str) else None
                st["idem"] = {ev["idem"]: [row[2], row[1]]} if row else {}
                before_reqs = {r: canon(q) for r, q in st["reqs"].items()}
                before_auth = canon({k: st[k] for k in ("principals", "authority", "admins", "revoked")})
                new, code = apply(st, ev)
                if code not in ("duplicate", "idem_conflict") and not row:
                    for r, q in new["reqs"].items():
                        if canon(q) != before_reqs.get(r):
                            conn.execute("update dc_reqs set doc=%s where req=%s", (json.dumps(q), r))
                    if canon({k: new[k] for k in ("principals", "authority", "admins", "revoked")}) != before_auth:
                        conn.execute("update dc_authority set doc=%s where id=1", (json.dumps({k: new[k] for k in ("principals", "authority", "admins", "revoked")}),))
                    if code != "bad_event":
                        conn.execute("insert into dc_events(idem, ev, code, fp, req) values (%s,%s,%s,%s,%s)",
                                     (ev["idem"], json.dumps(ev), code, sha(ev), ev.get("req") if isinstance(ev.get("req"), str) else None))
                if hook:
                    hook("before_commit", conn)
            return code
        except psycopg.errors.UniqueViolation:
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


def read_state(conn):
    a = conn.execute("select doc from dc_authority where id=1").fetchone()[0]
    reqs = {r: q for r, q in conn.execute("select req, doc from dc_reqs order by req").fetchall()}
    return dict(a, reqs=reqs)


def log_rows(conn):
    return [ev for (ev,) in conn.execute("select ev from dc_events order by seq").fetchall()]


def rebuild(conn, principals, authority, admins):
    st = genesis(principals, authority, admins)
    for ev in log_rows(conn):
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
        a = conn.execute("select doc from dc_authority where id=1").fetchone()[0]
        q = conn.execute("select doc from dc_reqs where req=%s", (ev["req"],)).fetchone()[0]
        st = dict(a, reqs={ev["req"]: q}, idem={})
        if hook:
            hook("after_read", conn)
        new, code = apply(st, ev)
        conn.execute("update dc_reqs set doc=%s where req=%s", (json.dumps(new["reqs"][ev["req"]]), ev["req"]))
        conn.execute("insert into dc_events(idem, ev, code, fp, req) values (%s,%s,%s,%s,%s)", (ev["idem"], json.dumps(ev), code, sha(ev), ev["req"]))
    return code
