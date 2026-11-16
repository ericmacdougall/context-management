"""All durable session state: appends to the systems write-path `events` table (append-only, per-session sequence,
hash chain; same algorithm as systems/memsys/store.py + chain.py), plus the `effects` idempotency table.
The engine never holds content; these functions are called by activities and by the API-side driver.

Multi-tenant rules: every function takes the tenant explicitly (no module constant), every key starts with the
tenant (keys.py), every lookup filters on tenant_id, and an event id is accepted for a session only when it is of the
same tenant and session and of the expected kind."""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import chain, keys
from .common import HOME, pg_connect

APPROVER_RE = re.compile(r"^[A-Za-z0-9_.@:\-]{1,80}$")

_pool = None
_pool_lock = threading.Lock()


class InvalidInput(Exception):
    """The referenced event is not a valid input for this tenant and session (never retry)."""


class RequestConflict(ValueError):
    """The same request id was reused with different content."""


class SubjectErased(Exception):
    """The subject's key was destroyed: the content is unreadable and no event can be appended (never retry)."""


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            os.environ["PGPASSFILE"] = str(HOME / "pg" / "pgpass")
            port = int((HOME / "pg" / "pg_port").read_text().strip())
            _pool = ConnectionPool(f"host=127.0.0.1 port={port} dbname=emlab user=emlab application_name=engine",
                                   min_size=2, max_size=int(os.environ.get("ENGINE_PG_POOL", "10")), open=True,
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
    # every event time comes from the Postgres clock, so API hosts and worker hosts cannot disagree about order
    ts = ev.get("ts") or cur.execute("select clock_timestamp()").fetchone()[0]
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


def append(tenant, sid, kind, content, actor="driver", trust="user_typed"):
    """Plain append (not idempotent: use append_input or append_once for anything an API can retry)."""
    keys.check_tenant(tenant)
    with pool().connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing",
                        (tenant, sid))
            return _append_locked(cur, tenant, sid, {"kind": kind, "content": content, "actor": actor, "trust": trust})


def pg_now_ms(conn=None):
    q = "select (extract(epoch from clock_timestamp()) * 1000)::bigint"
    if conn is not None:
        return conn.execute(q).fetchone()[0]
    with pool().connection() as c:
        return c.execute(q).fetchone()[0]


def fingerprint_of(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:32]


def append_once(tenant, key, sid, step, kind, content, actor="engine", trust="assistant", fingerprint=None):
    """Idempotent append: exactly one event per key, however many times this is called or retried. The effects row
    and the event commit in one transaction, so a crash leaves neither or both. Returns (event_id, created)."""
    keys.check_tenant(tenant)
    keys.check_part(sid, "session id")
    if not key.startswith(tenant + "/"):
        raise ValueError("idempotency key does not start with the tenant")
    with pool().connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing",
                        (tenant, sid))
            cur.execute("select 1 from sessions where tenant_id=%s and session_id=%s for update", (tenant, sid))
            got = cur.execute("insert into effects(key,tenant_id,session_id,step,status,fingerprint) "
                              "values (%s,%s,%s,%s,'done',%s) on conflict do nothing returning key",
                              (key, tenant, sid, step, fingerprint)).fetchone()
            if got is None:
                row = cur.execute("select result_ref, tenant_id, fingerprint from effects where key=%s", (key,)).fetchone()
                if row[1] != tenant:
                    raise ValueError("key owned by another tenant")
                if fingerprint is not None and row[2] is not None and row[2] != fingerprint:
                    raise RequestConflict("request id reused with different content")
                return row[0], False
            eid = _append_locked(cur, tenant, sid, {"kind": kind, "content": content, "actor": actor, "trust": trust})
            cur.execute("update effects set result_ref=%s where key=%s and tenant_id=%s", (eid, key, tenant))
            return eid, True


def append_input(tenant, sid, request_id, content):
    """Create a turn input exactly once per caller-supplied request id. Returns (event_id, created)."""
    return append_once(tenant, keys.input_key(tenant, sid, request_id), sid, "input", "turn_input", content,
                       "driver", "user_typed", fingerprint=fingerprint_of(content))


def _event_row(conn, tenant, sid, event_id):
    return conn.execute("select kind, ts_event, mem_dec(tenant_id, subject_id, content_enc) from events "
                        "where tenant_id=%s and session_id=%s and event_id=%s", (tenant, sid, event_id)).fetchone()


def check_turn_input(tenant, sid, event_id):
    """Reject anything that is not a turn_input of this tenant and session, before it is read or marked."""
    if not keys.belongs_to(tenant, sid, event_id):
        raise InvalidInput("event id is not of this tenant and session")
    with pool().connection() as conn:
        row = conn.execute("select kind from events where tenant_id=%s and session_id=%s and event_id=%s",
                           (tenant, sid, event_id)).fetchone()
    if row is None or row[0] != "turn_input":
        raise InvalidInput("not a turn_input event")


def read_digest(tenant, sid, event_id):
    """The 'agent turn' stand-in: read the input event from Postgres and derive a deterministic result digest."""
    check_turn_input(tenant, sid, event_id)
    if str(keys.parse_event_id(event_id)[2]) in os.environ.get("ENGINE_POISON_SEQS", "").split(","):
        raise RuntimeError("poison input (test knob ENGINE_POISON_SEQS)")      # a deterministic non-transient failure
    with pool().connection() as conn:
        row = _event_row(conn, tenant, sid, event_id)
    if row[2] is None:
        raise SubjectErased("content unreadable: subject key destroyed")
    return "d" + hashlib.sha256(row[2].encode()).hexdigest()[:16]


def has_older_unprocessed(tenant, sid, event_id):
    """True when an input with a lower seq has no recorded result yet (so this one must wait: sequence order)."""
    seq = keys.parse_event_id(event_id)[2]
    with pool().connection() as conn:
        row = conn.execute(
            "select 1 from events e where e.tenant_id=%s and e.session_id=%s and e.kind='turn_input' and e.seq < %s "
            "and not exists (select 1 from effects f where f.tenant_id=e.tenant_id and f.key = %s || e.event_id) limit 1",
            (tenant, sid, seq, keys.result_key_prefix(tenant))).fetchone()
    return row is not None


def maybe_crash(knob, where):
    """Targeted fault injection (test only): abrupt exit inside a critical window with the probability in the
    named test knob (a probability, not a secret)."""
    import random
    pr = float(os.environ.get(knob, "0") or 0)
    if pr and random.random() < pr:
        from . import activities
        with open(os.environ.get("ENGINE_CRASH_LOG", "/dev/null"), "a") as f:
            f.write(json.dumps({"t": time.time(), "where": where, "inflight": activities.inflight_snapshot()}) + "\n")
        os._exit(137)


def pause_if_asked():
    """Test knob (zombie worker): while the named file exists this activity thread holds its task (applied, not
    recorded), as a worker that was paused or partitioned would. It resumes when the file is removed."""
    path = os.environ.get("ENGINE_PAUSE_FILE")
    if path and os.path.exists(path):
        t0 = time.time()
        while os.path.exists(path) and time.time() - t0 < 600:
            time.sleep(0.2)
        with open(os.environ.get("ENGINE_CRASH_LOG", "/dev/null"), "a") as f:
            f.write(json.dumps({"t": time.time(), "how": "resumed-after-pause", "held_s": round(time.time() - t0, 1)}) + "\n")


def external_effect(tenant, sid, input_event_id, worker, gap_s=0.0, broken_keys=False, attempt=0):
    """One idempotent external effect. The external system dedups on the idempotency key; the effects row is the
    engine-side record. `gap_s` widens the window between 'applied' and 'recorded' so crash tests land in it.
    `broken_keys` derives the key from the attempt number (the bug the experiments must catch)."""
    if not keys.belongs_to(tenant, sid, input_event_id):
        raise InvalidInput("event id is not of this tenant and session")
    key = keys.effect_key(tenant, input_event_id)
    if broken_keys:
        key = f"{key}/a{attempt}"
    with pool().connection() as conn:
        conn.autocommit = True
        try:
            conn.execute("insert into ext_calls(key, worker, tenant_id) values (%s,%s,%s)", (key, worker, tenant))
            with conn.transaction():
                fresh = conn.execute("insert into ext_applied(key) values (%s) on conflict do nothing returning key",
                                     (key,)).fetchone()
                if fresh:
                    conn.execute("insert into effect_ledger(key, ref_event, tenant_id) values (%s,%s,%s)",
                                 (key, input_event_id, tenant))
            if gap_s:
                time.sleep(gap_s)
            maybe_crash("ENGINE_CRASH_IN_GAP", "effect_applied_not_recorded")
            pause_if_asked()
            conn.execute("insert into effects(key,tenant_id,session_id,step,status,result_ref) "
                         "values (%s,%s,%s,'effect','done',%s) on conflict do nothing",
                         (key, tenant, sid, input_event_id))
        finally:
            conn.autocommit = False
    return key


def unprocessed_inputs(tenant, sid, limit=20):
    """Input events with no recorded result (the doorbell-independent truth), in seq order."""
    with pool().connection() as conn:
        rows = conn.execute(
            "select e.event_id from events e where e.tenant_id=%s and e.session_id=%s and e.kind='turn_input' "
            "and not exists (select 1 from effects f where f.tenant_id=e.tenant_id and f.key = %s || e.event_id) "
            "order by e.seq limit %s", (tenant, sid, keys.result_key_prefix(tenant), limit)).fetchall()
    return [r[0] for r in rows]


def append_result(tenant, sid, input_event_id, digest, effect_key, code, attempt=0, broken=False):
    """Record the turn result once. Validates the input before it is marked processed."""
    check_turn_input(tenant, sid, input_event_id)
    rk = keys.result_key(tenant, input_event_id)
    if broken and attempt > 1:  # fence-off control: a RETRY derives a different key (the first attempt keeps the real one)
        rk = f"{rk}/a{attempt}"
    return append_once(tenant, rk, sid, "turn_result", "turn_result",
                       {"in": input_event_id, "digest": digest, "effect": effect_key, "code": code})   # (event id, created)


def dead_letter(tenant, sid, input_event_id, reason):
    """Poison path: the input counts as processed (reconcile skips it) and the reason is recorded. For an erased
    subject no event can be written (the key is gone), so only the effects row records it."""
    import psycopg
    check_turn_input(tenant, sid, input_event_id)       # never mark an id that is not an existing turn_input of this session
    key = keys.dead_key(tenant, input_event_id)
    try:
        eid, _ = append_once(tenant, key, sid, "dead_letter", "turn_dead_letter",
                             {"in": input_event_id, "reason": reason})
        return eid
    except psycopg.errors.Error as e:
        if "subject_erased" not in str(e):
            raise
    with pool().connection() as conn:
        conn.execute("insert into effects(key,tenant_id,session_id,step,status,result_ref) "
                     "values (%s,%s,%s,'dead_letter','dead',%s) on conflict do nothing", (key, tenant, sid, reason))
    return "effects-only"


# ------------------------------------------------------------------ approvals bound to one request id
def request_approval(tenant, sid, anchor, timeout_ms):
    """One request per gate anchor; the deadline is Postgres time plus the timeout. Returns (request_id, deadline_ms)
    as stored (a retry returns the first)."""
    key = keys.approval_request_key(tenant, sid, anchor)
    deadline_ms = pg_now_ms() + int(timeout_ms)
    eid, created = append_once(tenant, key, sid, "approval", "approval_requested",
                               {"deadline_ms": deadline_ms, "anchor": anchor})
    if created:
        return eid, deadline_ms
    return eid, _request_info(tenant, sid, eid)[0]


def _request_info(tenant, sid, request_id):
    with pool().connection() as conn:
        row = _event_row(conn, tenant, sid, request_id)
    if row is None or row[0] != "approval_requested" or row[2] is None:
        raise InvalidInput("no such approval request in this tenant and session")
    return json.loads(row[2])["deadline_ms"], row[1]


def grant_approval(tenant, sid, request_id, approver, now_ms=None):
    """Record a grant for ONE request, atomically with the checks that no resolution exists yet and that the Postgres
    clock is still before the deadline (`now_ms` is accepted for compatibility and IGNORED: a caller's clock is never
    trusted). Returns 'granted', 'duplicate' or 'rejected:<reason>'."""
    keys.check_tenant(tenant)
    keys.check_part(sid, "session id")
    if not isinstance(approver, str) or not APPROVER_RE.match(approver):
        return "rejected:approver"
    try:
        deadline_ms, _ = _request_info(tenant, sid, request_id)
    except InvalidInput:
        return "rejected:no_such_request"
    gkey = keys.approval_grant_key(tenant, request_id)
    with pool().connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("select 1 from sessions where tenant_id=%s and session_id=%s for update", (tenant, sid))
            if cur.execute("select 1 from effects where tenant_id=%s and key=%s",
                           (tenant, keys.approval_resolve_key(tenant, request_id))).fetchone():
                return "rejected:already_resolved"
            if pg_now_ms(conn) > deadline_ms:
                return "rejected:after_deadline"
            got = cur.execute("insert into effects(key,tenant_id,session_id,step,status) values (%s,%s,%s,'approval','done') "
                              "on conflict do nothing returning key", (gkey, tenant, sid)).fetchone()
            if got is None:
                return "duplicate"
            eid = _append_locked(cur, tenant, sid, {"kind": "approval_grant",
                                                    "content": {"request": request_id, "approver": approver},
                                                    "actor": "approver", "trust": "user_typed"})
            cur.execute("update effects set result_ref=%s where key=%s and tenant_id=%s", (eid, gkey, tenant))
            return "granted"


def resolve_approval(tenant, sid, request_id, now_ms=None):
    """Postgres decides, from the request event and the Postgres clock: granted if a grant bound to THIS request
    exists, was recorded after the request and no later than its deadline; timed out once the deadline has passed;
    else pending. (`now_ms` is accepted for compatibility and ignored.)"""
    deadline_ms, req_ts = _request_info(tenant, sid, request_id)
    with pool().connection() as conn:
        now_ms = pg_now_ms(conn)
        done = conn.execute("select result_ref from effects where tenant_id=%s and key=%s",
                            (tenant, keys.approval_resolve_key(tenant, request_id))).fetchone()
        if done:
            return conn.execute("select kind from events where tenant_id=%s and event_id=%s",
                                (tenant, done[0])).fetchone()[0]
        g = conn.execute("select result_ref from effects where tenant_id=%s and key=%s",
                         (tenant, keys.approval_grant_key(tenant, request_id))).fetchone()
        grant_ok = False
        if g and g[0]:
            row = _event_row(conn, tenant, sid, g[0])
            if row and row[0] == "approval_grant" and row[2] is not None:
                body = json.loads(row[2])
                grant_ok = (body.get("request") == request_id and bool(body.get("approver"))
                            and row[1] > req_ts and row[1].timestamp() * 1000 <= deadline_ms)
    if not grant_ok and now_ms < deadline_ms:
        return "pending"
    kind = "approval_granted" if grant_ok else "approval_timeout"
    eid, created = append_once(tenant, keys.approval_resolve_key(tenant, request_id), sid, "approval", kind,
                               {"request": request_id})
    if created:
        return kind
    with pool().connection() as conn:
        return conn.execute("select kind from events where tenant_id=%s and event_id=%s", (tenant, eid)).fetchone()[0]


def events_of(tenant, sid, kind=None):
    keys.check_tenant(tenant)
    with pool().connection() as conn:
        q = ("select seq, event_id, kind, mem_dec(tenant_id, subject_id, content_enc), ts_event from events "
             "where tenant_id=%s and session_id=%s " + ("and kind=%s " if kind else "") + "order by seq")
        return conn.execute(q, (tenant, sid) + ((kind,) if kind else ())).fetchall()


def verify_session_chain(tenant, sid):
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
