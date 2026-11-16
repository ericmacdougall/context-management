"""Database-time leases. Taking a lease bumps its epoch; the epoch is the fence token every effect presents.

The leases table is written only by the wp_lease_* SECURITY DEFINER functions (the app role has no privilege on it),
and this is the only module that calls them or names the table (exp/static_check.py S1 enforces it): a path that
re-read the current epoch just before writing would defeat the fence, so the token always comes from acquire().
Takeover waits on a lock at most lock_timeout_ms (default 2 s); a holder stuck inside a transaction is ended by the
role's idle_in_transaction_session_timeout (setup.py), so takeover is bounded (review F6)."""
import re
import threading
import time
import uuid

import psycopg

STALE = "WPS01"
TENANT_RX = re.compile(r"^[a-z0-9_]{1,40}$")


class LeaseLost(Exception):
    pass


def check_tenant(t):
    """API-boundary validation (also enforced by the tenant_t domain in every table)."""
    if not isinstance(t, str) or not TENANT_RX.match(t):
        raise ValueError("invalid tenant id")
    return t


class Lease:
    def __init__(self, tenant, scope, epoch, holder, acquired_at):
        self.tenant, self.scope, self.epoch, self.holder, self.acquired_at = tenant, scope, epoch, holder, acquired_at

    def __repr__(self):
        return f"Lease({self.tenant},{self.scope},e{self.epoch})"


def session_scope(session_id):
    return f"session:{session_id}"


PROJECTOR = "projector"


def acquire(conn, tenant, scope, ttl_s=30.0, force=False, holder=None, lock_timeout_ms=2000):
    """Take the lease if it is free or expired (DB clock), or unconditionally with force=True.
    Returns a Lease with the NEW epoch, or None if someone else holds a live lease. Raises psycopg LockNotAvailable
    if a current holder keeps the lease row locked longer than lock_timeout_ms (callers retry: acquire_wait)."""
    check_tenant(tenant)
    holder = holder or uuid.uuid4().hex
    with conn.transaction():
        r = conn.execute("select epoch, at from wp_lease_acquire(%s::tenant_t, %s, %s, %s, %s, %s)",
                         (tenant, scope, ttl_s, force, holder, lock_timeout_ms)).fetchone()
    return Lease(tenant, scope, r[0], holder, r[1]) if r else None


def acquire_wait(conn, tenant, scope, ttl_s=30.0, force=False, holder=None, deadline_s=30.0, lock_timeout_ms=2000):
    """acquire() retried on lock timeouts until deadline_s. Returns (Lease|None, attempts)."""
    t0, n = time.time(), 0
    while True:
        n += 1
        try:
            return acquire(conn, tenant, scope, ttl_s, force, holder, lock_timeout_ms), n
        except psycopg.errors.LockNotAvailable:
            if time.time() - t0 > deadline_s:
                return None, n


def renew(conn, lease, ttl_s=30.0):
    with conn.transaction():
        ok = conn.execute("select wp_lease_renew(%s::tenant_t, %s, %s, %s, %s)", (lease.tenant, lease.scope, lease.epoch, lease.holder, ttl_s)).fetchone()[0]
    if not ok:
        raise LeaseLost(lease)


def release(conn, lease):
    with conn.transaction():
        conn.execute("select wp_lease_release(%s::tenant_t, %s, %s, %s)", (lease.tenant, lease.scope, lease.epoch, lease.holder))


def is_stale_error(e):
    return isinstance(e, psycopg.Error) and getattr(e, "sqlstate", None) == STALE


TRANSIENT = (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled, psycopg.errors.SerializationFailure,
             psycopg.errors.DeadlockDetected, psycopg.OperationalError)


class Heartbeat:
    """Renews a lease (or, with renew_fn, any claim) every period_s on its own connection until stopped.
    `lost` is set if a renewal is refused (someone took over): the holder must stop.

    Default (session and projector leases, unchanged): ANY renewal error sets `lost`.
    tolerate_transient=True (opt-in; the erasure claim uses it): a transient failure (lock or statement timeout,
    serialization failure, dropped connection) is retried at the next beat, and `lost` is set only when the next
    attempt could not complete before the claim expires: now - last_ok + period_s + call_max_s >= ttl_s, where
    call_max_s bounds one renewal call (the renewing connection's lock_timeout). So the holder still stops before its
    claim can expire and be taken. Reason: at 7e8f28f the S100000 erasure lost its claim on ONE lock_timeout of the
    renewal against the job row, which the cascade batch held FOR UPDATE (PG log 07:46:29Z, item 2 of the 4960f50
    round)."""
    def __init__(self, renew_fn, period_s, ttl_s=None, tolerate_transient=False, call_max_s=0.0):
        self.renew_fn, self.period = renew_fn, period_s
        self.ttl = ttl_s or 3 * period_s
        self.tolerate, self.call_max = tolerate_transient, call_max_s
        self.lost = threading.Event()
        self._stop = threading.Event()
        self.beats, self.transient = 0, 0
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        last_ok = time.time()
        while not self._stop.wait(self.period):
            try:
                self.renew_fn()
                self.beats += 1
                last_ok = time.time()
            except TRANSIENT:
                self.transient += 1
                if not self.tolerate or time.time() - last_ok + self.period + self.call_max >= self.ttl:
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001  (a refusal: LeaseLost, ClaimLost, or anything unexpected)
                self.lost.set()
                return

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(5)


def lease_heartbeat(conn_factory, lease, ttl_s, period_s=None):
    c = conn_factory()
    return Heartbeat(lambda: renew(c, lease, ttl_s), period_s or ttl_s / 3)
