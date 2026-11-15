"""Database-time leases. Taking a lease bumps its epoch; the epoch is the fence token every effect presents.

This is the ONLY module that reads the leases table (tests/test_static.py enforces it): a path that re-read the
current epoch just before writing would defeat the fence, so the token always comes from acquire()."""
import uuid

import psycopg

STALE = "WPS01"


class LeaseLost(Exception):
    pass


class Lease:
    def __init__(self, tenant, scope, epoch, holder, acquired_at):
        self.tenant, self.scope, self.epoch, self.holder, self.acquired_at = tenant, scope, epoch, holder, acquired_at

    def __repr__(self):
        return f"Lease({self.tenant},{self.scope},e{self.epoch})"


def session_scope(session_id):
    return f"session:{session_id}"


PROJECTOR = "projector"


def acquire(conn, tenant, scope, ttl_s=30.0, force=False, holder=None, lock_timeout_ms=None):
    """Take the lease if it is free or expired (DB clock), or unconditionally with force=True (erasure, admin).
    Returns a Lease with the NEW epoch, or None if someone else holds a live lease."""
    holder = holder or uuid.uuid4().hex
    with conn.transaction():
        if lock_timeout_ms:
            conn.execute(f"set local lock_timeout = {int(lock_timeout_ms)}")
        conn.execute("insert into leases(tenant_id, scope) values (%s,%s) on conflict do nothing", (tenant, scope))
        r = conn.execute(
            "update leases set epoch = epoch + 1, holder = %s, expires_at = clock_timestamp() + make_interval(secs => %s) "
            "where tenant_id = %s and scope = %s and (%s or expires_at is null or expires_at < clock_timestamp()) "
            "returning epoch, clock_timestamp()", (holder, ttl_s, tenant, scope, force)).fetchone()
    return Lease(tenant, scope, r[0], holder, r[1]) if r else None


def acquire_in_txn(conn, tenant, scope, ttl_s, holder):
    """Forced acquisition inside the caller's transaction (erasure phase 1: tombstones and the bump commit together)."""
    conn.execute("insert into leases(tenant_id, scope) values (%s,%s) on conflict do nothing", (tenant, scope))
    r = conn.execute("update leases set epoch = epoch + 1, holder = %s, expires_at = clock_timestamp() + make_interval(secs => %s) "
                     "where tenant_id = %s and scope = %s returning epoch, clock_timestamp()", (holder, ttl_s, tenant, scope)).fetchone()
    return Lease(tenant, scope, r[0], holder, r[1])


def renew(conn, lease, ttl_s=30.0):
    r = conn.execute("update leases set expires_at = clock_timestamp() + make_interval(secs => %s) where tenant_id=%s and scope=%s and epoch=%s and holder=%s returning 1",
                     (ttl_s, lease.tenant, lease.scope, lease.epoch, lease.holder)).fetchone()
    if not r:
        raise LeaseLost(lease)


def release(conn, lease):
    conn.execute("update leases set expires_at = clock_timestamp() - interval '1 second' where tenant_id=%s and scope=%s and epoch=%s and holder=%s",
                 (lease.tenant, lease.scope, lease.epoch, lease.holder))


def expire_now(conn, tenant, scope):
    """Test/admin helper: mark a lease expired (models a holder that stalled past its TTL)."""
    conn.execute("update leases set expires_at = clock_timestamp() - interval '1 second' where tenant_id=%s and scope=%s", (tenant, scope))


def is_stale_error(e):
    return isinstance(e, psycopg.Error) and getattr(e, "sqlstate", None) == STALE
