"""Idempotency key derivation (pure). Every key starts with the tenant, so two tenants that happen to use the same
session id can never share, suppress or satisfy each other's keys. A key is a function of the LOGICAL step only
(tenant, session, the input event id, the scheduled rollup deadline, the approval request id) and never of the
attempt, run id, worker identity, process, time or a workflow counter: a retried or replayed activity, or a session
rebuilt in a fresh engine, derives the same key and so cannot repeat the effect."""
import re

TENANT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
ID_PART_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")     # session ids and request ids: no '/', so keys cannot be ambiguous


def check_tenant(tenant):
    if not isinstance(tenant, str) or not TENANT_RE.match(tenant):
        raise ValueError("bad tenant id")
    return tenant


def check_part(value, what="id"):
    if not isinstance(value, str) or not ID_PART_RE.match(value):
        raise ValueError(f"bad {what}")
    return value


def _k(tenant, *parts):
    check_tenant(tenant)
    return "/".join((tenant,) + tuple(str(p) for p in parts))


def effect_key(tenant, input_event_id: str) -> str:
    return _k(tenant, "eff", input_event_id)


def result_key(tenant, input_event_id: str) -> str:
    return _k(tenant, "res", input_event_id)


def result_key_prefix(tenant) -> str:
    return _k(tenant, "res") + "/"


def rollup_key(tenant, session_id: str, deadline_ms: int) -> str:
    """Keyed by the scheduled deadline of the rollup, which is stable identity (session start plus interval times n),
    not by how many rollups some counter believes have run."""
    return _k(tenant, "rollup", session_id, int(deadline_ms))


def approval_request_key(tenant, session_id: str, anchor: str) -> str:
    """One approval gate per anchor (the id of the event that triggered the gate)."""
    return _k(tenant, "approval", session_id, anchor, "request")


def approval_grant_key(tenant, request_id: str) -> str:
    """A grant is bound to exactly one request id."""
    return _k(tenant, "approval-grant", request_id)


def approval_resolve_key(tenant, request_id: str) -> str:
    return _k(tenant, "approval-resolve", request_id)


def finish_key(tenant, session_id: str) -> str:
    return _k(tenant, "finish", session_id)


def start_key(tenant, session_id: str) -> str:
    return _k(tenant, "start", session_id)


def input_key(tenant, session_id: str, request_id: str) -> str:
    """Input creation is idempotent by a caller-supplied request id (an API retry cannot create a second input).
    Session id and request id may not contain '/', so two (session, request) pairs cannot produce one key."""
    return _k(tenant, "input", check_part(session_id, "session id"), check_part(request_id, "request id"))


def dead_key(tenant, input_event_id: str) -> str:
    return result_key(tenant, input_event_id)  # a dead-lettered input counts as processed for reconcile


def parse_event_id(event_id):
    """'tenant/session/seq' -> (tenant, session, seq) or None when it is not a well-formed event id."""
    if not isinstance(event_id, str):
        return None
    parts = event_id.split("/")
    if len(parts) != 3 or not parts[2].isdigit() or not parts[1] or not TENANT_RE.match(parts[0]):
        return None
    return parts[0], parts[1], int(parts[2])


def belongs_to(tenant, session_id, event_id) -> bool:
    """True only for an event id of this tenant AND this session (checked before anything is read or marked)."""
    p = parse_event_id(event_id)
    return p is not None and p[0] == tenant and p[1] == session_id
