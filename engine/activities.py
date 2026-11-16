"""Activities: every touch of Postgres or the external system. Arguments and results are ids and small values only
(enforced by the payload codec). The tenant is an explicit argument of every activity (never a constant). All are safe
to retry or replay: keys derive from the logical step.

Failure classes: transient infrastructure errors (Postgres or pool unavailable) retry without bound; deterministic
failures (invalid input, erased subject) are non-retryable; any other error is retried a bounded number of times and
then becomes a non-retryable 'Poison' failure, which the workflow turns into a dead-letter record."""
import functools
import os
import threading

import psycopg
import psycopg_pool
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import keys, pgstore

POISON_AFTER_ATTEMPTS = 5
TRANSIENT = (psycopg.OperationalError, psycopg.InterfaceError, psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed)
# OperationalError subclasses that are NOT an outage: retrying them without bound only hides a deterministic failure
DETERMINISTIC = (psycopg.errors.QueryCanceled, psycopg.errors.DiskFull, psycopg.errors.OutOfMemory,
                 psycopg.errors.ProgramLimitExceeded, psycopg.errors.InsufficientResources)


def is_transient(e) -> bool:
    return isinstance(e, TRANSIENT) and not isinstance(e, DETERMINISTIC)


def classify_failure(e, attempt):
    """What the wrapper does with an exception: 'invalid' and 'erased' are non-retryable at once; 'transient' (an
    outage) is retried without bound; 'poison' is non-retryable after POISON_AFTER_ATTEMPTS; 'retry' is retried."""
    if isinstance(e, pgstore.InvalidInput):
        return "invalid"
    if isinstance(e, pgstore.SubjectErased):
        return "erased"
    if is_transient(e):
        return "transient"
    return "poison" if attempt >= POISON_AFTER_ATTEMPTS else "retry"


def note_transient(name, e):
    """Evidence for the Postgres-crash run: every transient database error an activity met (class, activity, time)."""
    path = os.environ.get("ENGINE_ERR_LOG")
    if path:
        import json
        import time
        with open(path, "a") as f:
            f.write(json.dumps({"t": time.time(), "activity": name, "class": type(e).__name__}) + "\n")

INFLIGHT = {}
_lk = threading.Lock()


def tracked(fn):
    """Counts activities currently executing in this process (read by the crash instrumentation) and applies the
    failure classes above."""
    @functools.wraps(fn)
    def inner(*a, **k):
        with _lk:
            INFLIGHT[fn.__name__] = INFLIGHT.get(fn.__name__, 0) + 1
        try:
            return fn(*a, **k)
        except ApplicationError:
            raise
        except Exception as e:
            kind = classify_failure(e, _attempt())
            if kind == "invalid":
                raise ApplicationError(str(e), type="InvalidInput", non_retryable=True)
            if kind == "erased":
                raise ApplicationError(str(e), type="SubjectErased", non_retryable=True)
            if kind == "transient":
                note_transient(fn.__name__, e)
                raise                      # an outage: retry without bound
            if kind == "poison":
                raise ApplicationError(f"{type(e).__name__}", type="Poison", non_retryable=True)
            raise
        finally:
            with _lk:
                INFLIGHT[fn.__name__] -= 1
    return inner


def _attempt():
    try:
        return activity.info().attempt
    except RuntimeError:   # called outside an activity (unit tests)
        return 0


def inflight_snapshot():
    with _lk:
        return {k: v for k, v in INFLIGHT.items() if v}


def _worker():
    return os.environ.get("ENGINE_WORKER_IDENT", "w?")


@activity.defn
@tracked
def turn_read(tenant: str, sid: str, event_id: str, skip: list) -> str:
    digest = pgstore.read_digest(tenant, sid, event_id)
    if pgstore.has_older_unprocessed(tenant, sid, event_id, skip):
        return "defer"        # an older input is unprocessed (its doorbell was lost): the workflow reconciles first
    return digest


@activity.defn
@tracked
def turn_policy(tenant: str, sid: str, event_id: str) -> str:  # a step added by code version 2
    return "ok"


@activity.defn
@tracked
def turn_audit(tenant: str, sid: str, event_id: str) -> str:  # a step added by code version 3
    return "ok"


@activity.defn
@tracked
def turn_effect(tenant: str, sid: str, event_id: str) -> str:
    return pgstore.external_effect(
        tenant, sid, event_id, _worker(), gap_s=float(os.environ.get("ENGINE_EFFECT_GAP", "0")),
        broken_keys=os.environ.get("ENGINE_BROKEN_KEYS") == "1", attempt=_attempt())


@activity.defn
@tracked
def turn_append(tenant: str, sid: str, event_id: str, digest: str, effect_key: str, code: str) -> list:
    eid, created = pgstore.append_result(tenant, sid, event_id, digest, effect_key, code, attempt=_attempt(),
                                         broken=os.environ.get("ENGINE_BROKEN_RESULT") == "1")
    pgstore.maybe_crash("ENGINE_CRASH_AFTER_APPEND", "result_committed_not_acked")
    # `counted`: this call created the result, OR it is a retry (attempt 2 or later: a first attempt may have created it
    # and lost its acknowledgement). A first attempt that finds an existing result is a late duplicate doorbell.
    return [eid, bool(created or _attempt() > 1)]


@activity.defn
@tracked
def reconcile(tenant: str, sid: str) -> list:
    return pgstore.unprocessed_inputs(tenant, sid, limit=20)


@activity.defn
@tracked
def dead_letter(tenant: str, sid: str, event_id: str, reason: str) -> str:
    if os.environ.get("ENGINE_FAIL_DEAD_LETTER") == "1":     # test knob: the dead-letter path itself fails
        raise RuntimeError("dead-letter store unavailable")
    return pgstore.dead_letter(tenant, sid, event_id, reason)


@activity.defn
@tracked
def rollup(tenant: str, sid: str, deadline_ms: int) -> str:
    eid, _ = pgstore.append_once(tenant, keys.rollup_key(tenant, sid, deadline_ms), sid, "rollup", "rollup",
                                 {"deadline_ms": int(deadline_ms)})
    return eid


@activity.defn
@tracked
def approval_request(tenant: str, sid: str, anchor: str, timeout_ms: int) -> list:
    rid, dl = pgstore.request_approval(tenant, sid, anchor, timeout_ms)
    return [rid, dl]


@activity.defn
@tracked
def approval_resolve(tenant: str, sid: str, request_id: str) -> str:
    """'approval_granted', 'approval_timeout' or 'pending', decided from Postgres for this request only."""
    return pgstore.resolve_approval(tenant, sid, request_id)


@activity.defn
@tracked
def finish_session(tenant: str, sid: str, turns: int) -> str:
    eid, _ = pgstore.append_once(tenant, keys.finish_key(tenant, sid), sid, "finish", "session_finished", {"turns": turns})
    return eid


@activity.defn
@tracked
def get_config(name: str) -> str:
    if os.environ.get("ENGINE_CONFIG_DELAY"):  # test knob: widens the window in which signals can arrive
        import time
        time.sleep(float(os.environ["ENGINE_CONFIG_DELAY"]))
    with pgstore.pool().connection() as conn:
        r = conn.execute("select value from engine_config where name=%s", (name,)).fetchone()
    return r[0] if r else ""


ALL = [turn_read, turn_policy, turn_audit, turn_effect, turn_append, reconcile, dead_letter, rollup, approval_request,
       approval_resolve, finish_session, get_config]
