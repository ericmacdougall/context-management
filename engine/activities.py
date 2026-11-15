"""Activities: every touch of Postgres or the external system. Arguments and results are ids and small values only
(enforced by the payload codec). All are safe to retry or replay: keys derive from the logical step."""
import functools
import json
import os
import threading

from temporalio import activity

from . import keys, pgstore


INFLIGHT = {}
_lk = threading.Lock()


def tracked(fn):
    """Counts activities currently executing in this process (read by the crash instrumentation, so a test can
    say whether an abrupt exit hit a worker with work in flight)."""
    @functools.wraps(fn)
    def inner(*a, **k):
        with _lk:
            INFLIGHT[fn.__name__] = INFLIGHT.get(fn.__name__, 0) + 1
        try:
            return fn(*a, **k)
        finally:
            with _lk:
                INFLIGHT[fn.__name__] -= 1
    return inner


def inflight_snapshot():
    with _lk:
        return {k: v for k, v in INFLIGHT.items() if v}


def _worker():
    return os.environ.get("ENGINE_WORKER_IDENT", "w?")


@activity.defn
@tracked
def turn_read(event_id: str) -> str:
    return pgstore.read_digest(event_id)


@activity.defn
@tracked
def turn_policy(event_id: str) -> str:  # a step added by code version 2
    return "ok"


@activity.defn
@tracked
def turn_audit(event_id: str) -> str:  # a step added by code version 3
    return "ok"


@activity.defn
@tracked
def turn_effect(sid: str, event_id: str) -> str:
    return pgstore.external_effect(
        sid, event_id, _worker(), gap_s=float(os.environ.get("ENGINE_EFFECT_GAP", "0")),
        broken_keys=os.environ.get("ENGINE_BROKEN_KEYS") == "1", attempt=activity.info().attempt)


@activity.defn
@tracked
def turn_append(sid: str, event_id: str, digest: str, effect_key: str, code: str) -> str:
    rk = keys.result_key(event_id)
    if os.environ.get("ENGINE_BROKEN_RESULT") == "1":  # fence-off control: key depends on the attempt
        rk = f"{rk}/a{activity.info().attempt}"
    eid, _ = pgstore.append_once(rk, sid, "turn_result", "turn_result",
                                 {"in": event_id, "digest": digest, "effect": effect_key, "code": code})
    pgstore.maybe_crash("ENGINE_CRASH_AFTER_APPEND", "result_committed_not_acked")
    return eid


@activity.defn
@tracked
def reconcile(sid: str) -> list:
    return pgstore.unprocessed_inputs(sid, limit=20)


@activity.defn
@tracked
def rollup(sid: str, ordinal: int) -> str:
    eid, _ = pgstore.append_once(keys.rollup_key(sid, ordinal), sid, "rollup", "rollup", {"k": ordinal})
    return eid


@activity.defn
@tracked
def approval_request(sid: str, deadline_ms: int) -> int:
    eid, created = pgstore.append_once(keys.approval_key(sid, "request"), sid, "approval", "approval_requested",
                                       {"deadline_ms": deadline_ms})
    if created:
        return deadline_ms
    for _, e, kind, content, _ in pgstore.events_of(sid, "approval_requested"):
        return json.loads(content)["deadline_ms"]
    return deadline_ms


@activity.defn
@tracked
def approval_resolve(sid: str, deadline_ms: int) -> str:
    """Postgres decides: granted if a grant event exists, timed out once the deadline has passed, else pending."""
    import time
    with pgstore.pool().connection() as conn:
        granted = conn.execute("select 1 from effects where key=%s", (keys.approval_key(sid, "grant"),)).fetchone()
        done = conn.execute("select result_ref from effects where key=%s", (keys.approval_key(sid, "resolve"),)).fetchone()
        if done:
            return conn.execute("select kind from events where event_id=%s", (done[0],)).fetchone()[0]
    if not granted and time.time() * 1000 < deadline_ms:
        return "pending"
    kind = "approval_granted" if granted else "approval_timeout"
    eid, created = pgstore.append_once(keys.approval_key(sid, "resolve"), sid, "approval", kind, {})
    if created:
        return kind
    with pgstore.pool().connection() as conn:
        return conn.execute("select kind from events where event_id=%s", (eid,)).fetchone()[0]


@activity.defn
@tracked
def finish_session(sid: str, turns: int) -> str:
    eid, _ = pgstore.append_once(keys.finish_key(sid), sid, "finish", "session_finished", {"turns": turns})
    return eid


@activity.defn
@tracked
def get_config(name: str) -> str:
    with pgstore.pool().connection() as conn:
        r = conn.execute("select value from engine_config where name=%s", (name,)).fetchone()
    return r[0] if r else ""


ALL = [turn_read, turn_policy, turn_audit, turn_effect, turn_append, reconcile, rollup, approval_request,
       approval_resolve, finish_session, get_config]
