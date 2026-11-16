"""Transaction-age watchdog (item 1 of the 4960f50 round).

Why: the projector of EVERY tenant reads only below the cluster's oldest running xid (row 23 fence), so one long
transaction anywhere delays every tenant's projection for its whole duration. statement_timeout bounds one statement
and idle_in_transaction_session_timeout bounds one idle gap, but on PostgreSQL 16 nothing bounds a transaction that
keeps issuing short statements (transaction_timeout arrives in PostgreSQL 17). This watchdog terminates any backend of
a MANAGED role whose transaction is older than max_s and holds an xid or a snapshot (backend_xid / backend_xmin).

Runs as wp_watchdog: pg_signal_backend + pg_read_all_stats, CONNECT only to the maintenance database, no table access.
pg_signal_backend cannot signal a superuser's backend: the admin role is NOT bounded by this (stated residual).
On PostgreSQL 17+ set transaction_timeout on every role instead (or as well).
usage: python -m wp.xact_watchdog [max_s]  (runs until killed; one line per termination, no query text)"""
import sys
import time

from . import db

MANAGED = (db.APP, db.VERIFIER, db.KMS, db.LEGAL)
MAX_XACT_S = 30.0


def sweep(conn, max_s=MAX_XACT_S):
    """Terminate managed backends whose transaction is older than max_s and holds an xid or snapshot. Returns
    [(pid, role, database, age_s)] for each backend terminated."""
    rows = conn.execute(
        "select pid, usename, datname, extract(epoch from clock_timestamp() - xact_start)::float8 from pg_stat_activity "
        "where usename = any(%s) and pid <> pg_backend_pid() and xact_start < clock_timestamp() - make_interval(secs => %s) "
        "and (backend_xid is not null or backend_xmin is not null)", (list(MANAGED), max_s)).fetchall()
    out = []
    for pid, role, dbn, age in rows:
        if conn.execute("select pg_terminate_backend(%s)", (pid,)).fetchone()[0]:
            out.append((pid, role, dbn, round(age, 1)))
    return out


def run(max_s=MAX_XACT_S, poll_s=1.0, stop=None):
    conn = db.watchdog()
    conn.execute("set statement_timeout = '5s'")
    while stop is None or not stop.is_set():
        for pid, role, dbn, age in sweep(conn, max_s):
            print(f"xact_watchdog terminated pid={pid} role={role} db={dbn} xact_age_s={age}", flush=True)
        time.sleep(poll_s)


if __name__ == "__main__":
    run(float(sys.argv[1]) if len(sys.argv) > 1 else MAX_XACT_S)
