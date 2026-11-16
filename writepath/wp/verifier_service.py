"""The verifier as its own service process (delta review D2).

The application process never constructs a Verifier when it runs with verifier="external": it leaves the job in
keys_destroyed (or logically_erased) and waits for the store to report done. This process, started with its own
credential directory (WP_SECRETS_DIR holding only pgpass.wp_verifier and, with D6, es_api_key.verifier.json), polls for
such jobs and completes them: expunge, cross-store counts, the unregistered-store gate, wp_erasure_complete.

Deployment requirement (not provable on one host with one OS user): this service runs under its own service identity,
the only identity able to read the verifier's credentials; the application's identity cannot. The lab proves the
narrower property exp/d2_verifier_process.py tests: an application process given only its own credential directory
cannot connect as the verifier or execute wp_erasure_complete, and the separate process completes the job.
usage: python -m wp.verifier_service [poll_s] [wait_s]   (runs until killed; prints job ids and outcomes only)"""
import sys
import time

from . import db
from .es import ES
from .verifier import Verifier


def pending(conn, tenant=None):
    return [r[0] for r in conn.execute(
        "select job_id from erasure_jobs where state in ('keys_destroyed', 'logically_erased') and (%s::text is null or tenant_id = %s) "
        "order by updated_at", (tenant, tenant)).fetchall()]


def once(v, conn, wait_s=2.0, tenant=None):
    out = {}
    for jid in pending(conn, tenant):
        try:
            r = v.complete(jid, wait_s=wait_s)
            out[jid] = r.get("receipt_state", "done" if r.get("done") else "residue")
        except Exception as e:  # noqa: BLE001  (one job's failure never stops another)
            out[jid] = f"error {getattr(e, 'sqlstate', None) or type(e).__name__}"
    return out


def run(poll_s=1.0, wait_s=2.0, stop=None, tenant=None):
    v = Verifier(ES(role="verifier"))
    conn = db.verifier()
    while stop is None or not stop.is_set():
        for jid, st in once(v, conn, wait_s, tenant).items():
            print(f"verifier_service job={jid} -> {st}", flush=True)
        time.sleep(poll_s)


if __name__ == "__main__":
    run(float(sys.argv[1]) if len(sys.argv) > 1 else 1.0, float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
