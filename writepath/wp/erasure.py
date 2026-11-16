"""Erasure of one data subject: speaker provenance (the subject's own events and everything derived from them).

  request()   durable record first; one open job per (tenant, subject) (a second request returns it: same subject
              serialises, different subjects run concurrently and independently).
  claim       a runner claims the job (claim_epoch+1) and a heartbeat renews the claim; a crashed runner's job is
              picked up by sweep() after the claim expires.
  mark        one short transaction: the subject row FOR UPDATE, erased=true. From its commit every content insert
              for the subject is refused by the store (WPS03) and every reader and the projector filter the subject.
  cascade     bounded batches (own transactions): tombstones for the subject's events, delete derived facts, analyst
              jobs, inbox and quarantine rows. No long transaction, so no long lock hold and no long xmin hold.
  placeholders  for every tombstoned event id (paged from Postgres, never from ES): a content-free placeholder at the
              event's _id and at the derived fact's _id (external version 2^62). A stale projector write that arrives
              later is refused by Elasticsearch (409), one that arrived earlier is overwritten (families.py).
              The projector lease is NOT taken: projection of every other subject continues.
  keys        the subject key is destroyed (key service).
  complete    the VERIFIER (role wp_verifier, own connections, own ES client) runs the expunge, counts the
              cross-store residue itself, runs the unregistered-store gate, and calls wp_erasure_complete, which
              recomputes every Postgres count in the store and writes 'done' only if all are zero.
Every step is idempotent; any exception leaves the job open with its error TYPE recorded; the claim is always
released; sweep() isolates jobs from each other."""
import time
import uuid

import psycopg

from . import db, families as F, keys as K, lease as L
from .verifier import Verifier, SCOPE


def _noop(_n, _data=None):
    pass


class ClaimLost(Exception):
    pass


def request(conn, tenant, subject_id):
    """Job id of the open erasure job for (tenant, subject), creating it if none is open."""
    L.check_tenant(tenant)
    for _ in range(3):
        r = conn.execute("select job_id from erasure_jobs where tenant_id=%s and subject_id=%s and state <> 'done'", (tenant, subject_id)).fetchone()
        if r:
            return r[0]
        jid = uuid.uuid4().hex[:16]
        try:
            conn.execute("insert into erasure_jobs(job_id, tenant_id, subject_id) values (%s,%s,%s)", (jid, tenant, subject_id))
            return jid
        except psycopg.errors.UniqueViolation:
            continue
    raise RuntimeError("could not create or find the open job")


def _claim(conn, jid, ttl_s):
    r = conn.execute("update erasure_jobs set claim_epoch = claim_epoch + 1, claim_expires = clock_timestamp() + make_interval(secs => %s), "
                     "attempts = attempts + 1, updated_at = clock_timestamp() where job_id = %s and state <> 'done' "
                     "and (claim_expires is null or claim_expires < clock_timestamp()) returning claim_epoch", (ttl_s, jid)).fetchone()
    return r[0] if r else None


def _renew(conn, jid, ce, ttl_s):
    r = conn.execute("update erasure_jobs set claim_expires = clock_timestamp() + make_interval(secs => %s) where job_id=%s and claim_epoch=%s "
                     "and state <> 'done' returning 1", (ttl_s, jid, ce)).fetchone()
    if not r:
        raise ClaimLost(jid)


def _release(conn, jid, ce, err=None):
    conn.execute("update erasure_jobs set claim_expires = clock_timestamp() - interval '1 second', last_error_type = %s, updated_at = clock_timestamp() "
                 "where job_id=%s and claim_epoch=%s and state <> 'done'", (err, jid, ce))


def _state(conn, jid, ce, state):
    r = conn.execute("update erasure_jobs set state=%s, updated_at=clock_timestamp() where job_id=%s and claim_epoch=%s and state <> 'done' returning 1",
                     (state, jid, ce)).fetchone()
    if not r:
        raise ClaimLost(jid)


def _job(conn, jid):
    r = conn.execute("select tenant_id, subject_id, state from erasure_jobs where job_id=%s", (jid,)).fetchone()
    return dict(zip(("tenant", "subject", "state"), r))


def tombstoned_ids(conn, t, s, page=10000):
    """Yield pages of (event_id, kind) for the subject's tombstoned events, keyset-paged (bounded memory)."""
    last = ""
    while True:
        rows = conn.execute("select tb.event_id, e.kind from tombstones tb join events e on e.tenant_id = tb.tenant_id and e.event_id = tb.event_id "
                            "where tb.tenant_id=%s and tb.subject_id=%s and tb.event_id > %s order by tb.event_id limit %s", (t, s, last, page)).fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1][0]


def run(conn, ks: K.KeyService, es, jid, ttl_s=60.0, hook=_noop, verifier=None, batch=5000, destroy_key=True, placeholders=True):
    """Drive one job as far as it goes. Never raises: returns a receipt with done and error_type."""
    t0 = time.time()
    j = _job(conn, jid)
    t, s = j["tenant"], j["subject"]
    rec = {"job_id": jid, "tenant": t, "subject": s, "done": False, "error_type": None, "steps": {}}
    if j["state"] == "done":
        rec["done"] = True
        return rec
    ce = _claim(conn, jid, ttl_s)
    if ce is None:
        rec["error_type"] = "claimed_elsewhere"
        return rec
    hconn = db.connect(application_name="wp-erasure-hb")
    hb = L.Heartbeat(lambda: _renew(hconn, jid, ce, ttl_s), ttl_s / 3)
    err = None
    try:
        with hb:
            ts = time.time()
            conn.execute("select wp_erasure_mark(%s)", (jid,))
            rec["steps"]["mark_s"] = round(time.time() - ts, 3)
            hook("after_mark")
            ts, n, batches = time.time(), 1, 0
            while n:
                if hb.lost.is_set():
                    raise ClaimLost(jid)
                n = conn.execute("select wp_erasure_cascade(%s, %s)", (jid, batch)).fetchone()[0]
                batches += 1
            rec["steps"].update(cascade_s=round(time.time() - ts, 3), cascade_batches=batches)
            hook("after_cascade")
            ts, ev_n, fa_n = time.time(), 0, 0
            if placeholders:
                for page in tombstoned_ids(conn, t, s):
                    if hb.lost.is_set():
                        raise ClaimLost(jid)
                    F.place_placeholders(es, t, "events", [e for e, _ in page])
                    F.place_placeholders(es, t, "facts", [K.derive_id(t, e) for e, k in page if k == "fact"])
                    ev_n += len(page)
                    fa_n += sum(1 for _, k in page if k == "fact")
            rec["steps"].update(placeholders_s=round(time.time() - ts, 3), placeholder_events=ev_n, placeholder_facts=fa_n)
            hook("after_placeholders")
            _state(conn, jid, ce, "purged")
            if destroy_key:
                ts = time.time()
                ks.destroy(t, s)
                rec["steps"]["key_destroy_s"] = round(time.time() - ts, 3)
            _state(conn, jid, ce, "keys_destroyed")
            hook("before_complete")
            ts = time.time()
            v = verifier or Verifier()
            res = v.complete(jid)
            rec["steps"]["verify_s"] = round(time.time() - ts, 3)
            rec["steps"]["expunge"] = res.get("expunge")
            rec.update(done=bool(res["done"]), verify=res.get("verify"), residue=res.get("residue"), scope=SCOPE)
            if not res["done"]:
                err = "residue"
    except Exception as e:  # noqa: BLE001  (any failure leaves the job open with its type recorded)
        err = type(e).__name__
    finally:
        try:
            _release(conn, jid, ce, err)
        except psycopg.Error:
            pass
        hconn.close()
    rec["error_type"] = err
    rec["seconds"] = round(time.time() - t0, 3)
    return rec


def erase(conn, ks, es, tenant, subject_id, **kw):
    return run(conn, ks, es, request(conn, tenant, subject_id), **kw)


def sweep(conn, ks, es, **kw):
    """Run every open job whose claim is free or expired. One job's failure never stops another."""
    out = {}
    for (jid,) in conn.execute("select job_id from erasure_jobs where state <> 'done' and (claim_expires is null or claim_expires < clock_timestamp()) "
                               "order by created_at").fetchall():
        try:
            out[jid] = run(conn, ks, es, jid, **kw)["done"]
        except Exception as e:  # noqa: BLE001
            out[jid] = f"error {type(e).__name__}"
    return out
