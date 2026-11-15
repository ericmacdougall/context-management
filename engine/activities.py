"""Activities: every touch of Postgres or the external system. Arguments and results are ids and small values only
(enforced by the payload codec). All are safe to retry or replay: keys derive from the logical step."""
import json
import os

from temporalio import activity

from . import keys, pgstore


def _worker():
    return os.environ.get("ENGINE_WORKER_IDENT", "w?")


@activity.defn
def turn_read(event_id: str) -> str:
    return pgstore.read_digest(event_id)


@activity.defn
def turn_policy(event_id: str) -> str:  # a step added by code version 2
    return "ok"


@activity.defn
def turn_audit(event_id: str) -> str:  # a step added by code version 3
    return "ok"


@activity.defn
def turn_effect(sid: str, event_id: str) -> str:
    return pgstore.external_effect(
        sid, event_id, _worker(), gap_s=float(os.environ.get("ENGINE_EFFECT_GAP", "0")),
        broken_keys=os.environ.get("ENGINE_BROKEN_KEYS") == "1", attempt=activity.info().attempt)


@activity.defn
def turn_append(sid: str, event_id: str, digest: str, effect_key: str, code: str) -> str:
    eid, _ = pgstore.append_once(keys.result_key(event_id), sid, "turn_result", "turn_result",
                                 {"in": event_id, "digest": digest, "effect": effect_key, "code": code})
    return eid


@activity.defn
def reconcile(sid: str) -> list:
    return pgstore.unprocessed_inputs(sid, limit=20)


@activity.defn
def rollup(sid: str, ordinal: int) -> str:
    eid, _ = pgstore.append_once(keys.rollup_key(sid, ordinal), sid, "rollup", "rollup", {"k": ordinal})
    return eid


@activity.defn
def approval_request(sid: str, deadline_ms: int) -> int:
    eid, created = pgstore.append_once(keys.approval_key(sid, "request"), sid, "approval", "approval_requested",
                                       {"deadline_ms": deadline_ms})
    if created:
        return deadline_ms
    for _, e, kind, content, _ in pgstore.events_of(sid, "approval_requested"):
        return json.loads(content)["deadline_ms"]
    return deadline_ms


@activity.defn
def approval_resolve(sid: str) -> str:
    from .common import TENANT
    with pgstore.pool().connection() as conn:
        granted = conn.execute("select 1 from effects where key=%s", (keys.approval_key(sid, "grant"),)).fetchone()
    kind = "approval_granted" if granted else "approval_timeout"
    eid, created = pgstore.append_once(keys.approval_key(sid, "resolve"), sid, "approval", kind, {})
    if created:
        return kind
    with pgstore.pool().connection() as conn:
        return conn.execute("select kind from events where event_id=%s", (eid,)).fetchone()[0]


@activity.defn
def finish_session(sid: str, turns: int) -> str:
    eid, _ = pgstore.append_once(keys.finish_key(sid), sid, "finish", "session_finished", {"turns": turns})
    return eid


@activity.defn
def get_config(name: str) -> str:
    with pgstore.pool().connection() as conn:
        r = conn.execute("select value from engine_config where name=%s", (name,)).fetchone()
    return r[0] if r else ""


ALL = [turn_read, turn_policy, turn_audit, turn_effect, turn_append, reconcile, rollup, approval_request,
       approval_resolve, finish_session, get_config]
