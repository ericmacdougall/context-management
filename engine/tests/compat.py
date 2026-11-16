"""Adapter so ONE set of review-fix tests runs against both code versions: the new multi-tenant API and the old
single-tenant API at 62916ae. The tests assert the required behaviour; on the old code they fail for the reason
the review gave. Needs the engine Postgres (run on the host)."""
import json
import time
import uuid

from engine import activities, keys, pgstore
from engine.common import pg_connect

NEW = hasattr(pgstore, "grant_approval")
NEWER = hasattr(pgstore, "pg_now_ms")      # delta-review code: the approval deadline is Postgres time plus a timeout


def sid():
    return "rv" + uuid.uuid4().hex[:10]


def make_input(tenant, s, request_id, n=1):
    if NEW:
        return pgstore.append_input(tenant, s, request_id, {"n": n, "text": f"input {n}"})[0]
    return pgstore.append(s, "turn_input", {"n": n, "text": f"input {n}"}, tenant=tenant)


def request(tenant, s, anchor, deadline_ms):
    """Returns a request id (new code) or None (old code has no request ids)."""
    if NEW:
        if NEWER:
            return pgstore.request_approval(tenant, s, anchor, deadline_ms - now_ms())[0]   # timeout from now (Postgres time)
        return pgstore.request_approval(tenant, s, anchor, deadline_ms)[0]
    if tenant != "t1":      # the old activities hard-code the tenant: write the request the way they would
        pgstore.append_once(keys.approval_key(s, "request"), s, "approval", "approval_requested",
                            {"deadline_ms": deadline_ms}, tenant=tenant)
        return None
    activities.approval_request(s, deadline_ms)
    return None


def grant(tenant, s, request_id, approver="approver-1", now_ms=None):
    """The old API recorded any grant for the session, unconditionally."""
    if NEW:
        return pgstore.grant_approval(tenant, s, request_id, approver, now_ms)
    pgstore.append_once(keys.approval_key(s, "grant"), s, "approval", "approval_grant", {}, tenant=tenant)
    return "granted"


def resolve(tenant, s, request_id, deadline_ms):
    """Normalised to 'granted' | 'timeout' | 'pending'."""
    if NEW:
        r = pgstore.resolve_approval(tenant, s, request_id)
    else:
        r = activities.approval_resolve(s, deadline_ms)
    return {"approval_granted": "granted", "approval_timeout": "timeout", "pending": "pending"}[r]


def finish(tenant, s):
    if NEW:
        return pgstore.append_once(tenant, keys.finish_key(tenant, s), s, "finish", "session_finished", {"turns": 1})
    return pgstore.append_once(keys.finish_key(s), s, "finish", "session_finished", {"turns": 1}, tenant=tenant)


def rollup(tenant, s, ordinal, deadline_ms):
    """New: keyed by the scheduled deadline. Old: keyed by a counter (the caller's ordinal)."""
    if NEW:
        return activities.rollup(tenant, s, deadline_ms)
    return activities.rollup(s, ordinal)


def read_digest(tenant, s, event_id):
    return pgstore.read_digest(tenant, s, event_id) if NEW else activities.turn_read(event_id)


def append_result(tenant, s, event_id):
    if NEW:
        return activities.turn_append(tenant, s, event_id, "d0", "k0", "test")
    return activities.turn_append(s, event_id, "d0", "k0", "test")


def unprocessed(tenant, s):
    return pgstore.unprocessed_inputs(tenant, s) if NEW else pgstore.unprocessed_inputs(s, tenant=tenant)


def events(tenant, s, kind):
    return pgstore.events_of(tenant, s, kind) if NEW else pgstore.events_of(s, kind, tenant=tenant)


def dead_letter(tenant, s, event_id, reason="InvalidInput"):
    return pgstore.dead_letter(tenant, s, event_id, reason) if NEW else activities.dead_letter(s, event_id, reason) \
        if hasattr(activities, "dead_letter") else None


def counted(result):
    """Did the turn_append result say the turn is newly counted? Code that does not say counts every call."""
    return result[1] if isinstance(result, list) else True


def erase_subject(tenant, s):
    with pg_connect(autocommit=True) as c:
        c.execute("update keys set key=null, destroyed_at=now() where tenant_id=%s and subject_id=%s",
                  (tenant, pgstore.subject_of(s)))


def now_ms():
    return int(time.time() * 1000)
