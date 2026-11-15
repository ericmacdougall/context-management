"""All durable session state: appends to the systems write-path `events` table (append-only, per-session sequence,
hash chain; same algorithm as systems/memsys/store.py + chain.py), plus the `effects` idempotency table.
The engine never holds content; these functions are called by activities and by the API-side driver."""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from . import chain, keys
from .common import HOME, TENANT, pg_connect

_pool = None
_pool_lock = threading.Lock()


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            os.environ["PGPASSFILE"] = str(HOME / "pg" / "pgpass")
            port = int((HOME / "pg" / "pg_port").read_text().strip())
            _pool = ConnectionPool(f"host=127.0.0.1 port={port} dbname=emlab user=emlab application_name=engine",
                                   min_size=2, max_size=int(os.environ.get("ENGINE_PG_POOL", "24")), open=True,
                                   timeout=30)
        return _pool


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def subject_of(sid):
    return f"u-{sid}"


def _append_locked(cur, tenant, sid, ev):
    """Append one event; caller holds the transaction and has locked the sessions row."""
    next_seq, last_hash = cur.execute(
        "select next_seq, last_hash from sessions where tenant_id=%s and session_id=%s for update",
        (tenant, sid)).fetchone()
    enc = cur.execute("select mem_enc(%s,%s,%s)", (tenant, subject_of(sid), json.dumps(ev["content"], sort_keys=True))
                      ).fetchone()[0]
    ts = ev.get("ts") or datetime.now(timezone.utc)
    eid = f"{tenant}/{sid}/{next_seq}"
    h = chain.event_hash(last_hash, tenant, sid, next_seq, eid, _iso(ts), ev["actor"], ev["trust"], ev["kind"],
                         subject_of(sid), enc)
    cur.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,"
                "content_enc,prev_hash,hash) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant, sid, next_seq, eid, ts, subject_of(sid), ev["actor"], ev["trust"], ev["kind"], enc,
                 last_hash, h))
    cur.execute("update sessions set next_seq=%s, last_hash=%s where tenant_id=%s and session_id=%s",
                (next_seq + 1, h, tenant, sid))
    return eid


def append(sid, kind, content, actor="driver", trust="user_typed", tenant=TENANT):
    """Plain append (driver side: a user turn or approval is an event before it is ever a signal)."""
    with pool().connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing",
                        (tenant, sid))
            return _append_locked(cur, tenant, sid, {"kind": kind, "content": content, "actor": actor, "trust": trust})


def append_once(key, sid, step, kind, content, actor="engine", trust="assistant", tenant=TENANT):
    """Idempotent append: exactly one event per key, however many times this is called or retried. The effects row
    and the event commit in one transaction, so a crash leaves neither or both. Returns (event_id, created)."""
    with pool().connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing",
                        (tenant, sid))
            cur.execute("select 1 from sessions where tenant_id=%s and session_id=%s for update", (tenant, sid))
            got = cur.execute("insert into effects(key,tenant_id,session_id,step,status) values (%s,%s,%s,%s,'done') "
                              "on conflict do nothing returning key", (key, tenant, sid, step)).fetchone()
            if got is None:
                ref = cur.execute("select result_ref from effects where key=%s", (key,)).fetchone()[0]
                return ref, False
            eid = _append_locked(cur, tenant, sid, {"kind": kind, "content": content, "actor": actor, "trust": trust})
            cur.execute("update effects set result_ref=%s where key=%s", (eid, key))
            return eid, True


def read_digest(event_id):
    """The 'agent turn' stand-in: read the input event from Postgres and derive a deterministic result digest."""
    tenant, sid, _ = event_id.split("/")
    with pool().connection() as conn:
        row = conn.execute("select mem_dec(tenant_id, subject_id, content_enc) from events where event_id=%s",
                           (event_id,)).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"no such event {event_id}")
    return "d" + hashlib.sha256(row[0].encode()).hexdigest()[:16]


def maybe_crash(knob, where):
    """Targeted fault injection (test only): abrupt exit inside a critical window with the probability in the
    named test knob (a probability, not a secret)."""
    import json
    import random
    import time as _t
    pr = float(os.environ.get(knob, "0") or 0)
    if pr and random.random() < pr:
        from . import activities
        with open(os.environ.get("ENGINE_CRASH_LOG", "/dev/null"), "a") as f:
            f.write(json.dumps({"t": _t.time(), "where": where, "inflight": activities.inflight_snapshot()}) + "\n")
        os._exit(137)


def external_effect(sid, input_event_id, worker, gap_s=0.0, broken_keys=False, attempt=0):
    """One idempotent external effect. The external system dedups on the idempotency key; the effects row is the
    engine-side record. `gap_s` widens the window between 'applied' and 'recorded' so crash tests land in it.
    `broken_keys` derives the key from the attempt number (the bug the experiments must catch)."""
    import time
    key = keys.effect_key(input_event_id)
    if broken_keys:
        key = f"{key}/a{attempt}"
    with pool().connection() as conn:
      conn.autocommit = True
      try:
        conn.execute("insert into ext_calls(key, worker) values (%s,%s)", (key, worker))
        with conn.transaction():
            fresh = conn.execute("insert into ext_applied(key) values (%s) on conflict do nothing returning key",
                                 (key,)).fetchone()
            if fresh:
                conn.execute("insert into effect_ledger(key, ref_event) values (%s,%s)", (key, input_event_id))
        if gap_s:
            time.sleep(gap_s)
        maybe_crash("ENGINE_CRASH_IN_GAP", "effect_applied_not_recorded")
        conn.execute("insert into effects(key,tenant_id,session_id,step,status,result_ref) values (%s,%s,%s,'effect','done',%s) "
                     "on conflict do nothing", (key, TENANT, sid, input_event_id))
      finally:
        conn.autocommit = False
    return key


def unprocessed_inputs(sid, limit=20, tenant=TENANT):
    """Input events with no recorded result (the doorbell-independent truth), in seq order."""
    with pool().connection() as conn:
        rows = conn.execute(
            "select e.event_id from events e where e.tenant_id=%s and e.session_id=%s and e.kind='turn_input' "
            "and not exists (select 1 from effects f where f.key = 'res/' || e.event_id) order by e.seq limit %s",
            (tenant, sid, limit)).fetchall()
    return [r[0] for r in rows]


def events_of(sid, kind=None, tenant=TENANT):
    with pool().connection() as conn:
        q = ("select seq, event_id, kind, mem_dec(tenant_id, subject_id, content_enc), ts_event from events "
             "where tenant_id=%s and session_id=%s " + ("and kind=%s " if kind else "") + "order by seq")
        return conn.execute(q, (tenant, sid) + ((kind,) if kind else ())).fetchall()


def verify_session_chain(sid, tenant=TENANT):
    with pg_connect() as conn:
        cur = conn.cursor()
        cur.execute("select tenant_id,session_id,seq,event_id,ts_event,actor,trust_origin,kind,subject_id,content_enc,"
                    "prev_hash,hash from events where tenant_id=%s and session_id=%s order by seq", (tenant, sid))
        cols = [d.name for d in cur.description]
        rows = []
        for r in cur:
            d = dict(zip(cols, r))
            d["ts_event"] = _iso(d["ts_event"])
            d["content_enc"] = bytes(d["content_enc"])
            rows.append(d)
    return chain.verify_chain(rows)
