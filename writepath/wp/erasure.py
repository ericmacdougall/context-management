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
import json
import time
import uuid

import psycopg

from . import db, families as F, keys as K, lease as L, mentions as M
from .verifier import Verifier


BATCH_TARGET_S = 2.0
HB_TOLERATE = True          # exp/scale_1m.py H control sets False (the 7e8f28f behaviour)
PHYS_INLINE_S = 2.0         # physical="followup": one short purge attempt before returning the logical receipt
PHYS_WAIT_S = 60.0          # physical="wait" and the sweep follow-up: per attempt


def _noop(_n, _data=None):
    pass


class ClaimLost(Exception):
    pass


class Withheld(Exception):
    """Events in scope are covered by another subject's legal hold; the job waits for its release."""


class CascadeDeadline(Exception):
    pass


class KeyPurgePending(Exception):
    """The key row is deleted but its bytes are not proven gone from disk (purge timed out); the job stays open."""


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
    """Advance the job's state (forward only, the store's transition rule): a resumed job already past `state` stays."""
    r = conn.execute("update erasure_jobs set state = case when wp_erasure_rank(state) < wp_erasure_rank(%s) then %s else state end, "
                     "updated_at=clock_timestamp() where job_id=%s and claim_epoch=%s and state <> 'done' returning 1",
                     (state, state, jid, ce)).fetchone()
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


def _holds(conn, t, s):
    return [dict(zip(("hold_id", "placed_at", "released_at"), (h, str(p), str(r) if r else None))) for h, p, r in conn.execute(
        "select hold_id, placed_at, released_at from legal_holds where tenant_id=%s and subject_id=%s order by placed_at", (t, s)).fetchall()]


def rescan(conn, ks, jid, page=2000):
    """Erasure-time mention rescan: events of OTHER speakers written against an identifier-set version older than the
    subject's (declared after they were written) are decrypted and matched against the subject's identifier HMACs.
    Hits are recorded in erasure_mentions (the cascade then removes them); the covered version is recorded on the job.
    Returns (scanned, found). Undecryptable content (speaker already erased) has nothing left to leak and is skipped."""
    t, s, xmax, done_x = conn.execute("select tenant_id, subject_id, ident_xmax, rescanned_xmax from erasure_jobs where job_id=%s", (jid,)).fetchone()
    if not xmax or (done_x is not None and done_x >= xmax):
        return 0, 0
    hm = {r[0] for r in conn.execute("select ident_hmac from subject_identifiers where tenant_id=%s and subject_id=%s", (t, s)).fetchall()}
    last, scanned, found = "", 0, 0
    while True:
        rows = conn.execute("select event_id, subject_id, content_enc from events where tenant_id=%s and subject_id <> %s and content_enc is not null "
                            "and mention_scan_upto < %s and not (mentions @> array[%s]) and event_id > %s order by event_id limit %s",
                            (t, s, xmax, s, last, page)).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        keys = ks.keys(t, [r[1] for r in rows])
        texts = {}
        for eid, sp, enc in rows:
            plain = K.open_(keys[sp], t, sp, eid, enc)
            if plain is not None:
                texts[eid] = sorted(M.windows(plain))
        flat = sorted({w for v in texts.values() for w in v})
        h = dict(zip(flat, ks.tenant_hmac_many(t, ["ident:" + w for w in flat]))) if flat else {}
        hits = [eid for eid, ws in texts.items() if any(h[w] in hm for w in ws)]
        if hits:
            conn.execute("select wp_erasure_add_mentions(%s, %s)", (jid, hits))
        scanned += len(rows)
        found += len(hits)
    conn.execute("select wp_erasure_rescan_done(%s, %s)", (jid, xmax))
    return scanned, found


def run(conn, ks: K.KeyService, es, jid, ttl_s=60.0, hook=_noop, verifier=None, batch=5000, destroy_key=True, placeholders=True, do_rescan=True,
        cascade_deadline_s=None, physical="followup"):
    """Drive one job as far as it goes. Never raises: returns a receipt with done, receipt_state and error_type. A legal
    hold leaves the job queued (error_type 'legal_hold', held=True) and it runs once the hold is released (release_hold).

    Item 4 (physical purge): once every LOGICAL count is zero the store records 'logically_erased' with a deadline;
    'done' needs the physical purge too (ES soft-deleted documents merged away after the retention lease passes, the
    key's bytes rewritten away past the xid horizon). physical="followup" (default) makes one short attempt
    (PHYS_INLINE_S) and returns receipt_state 'logically_erased_physical_purge_pending' if the purge is not yet possible,
    so a small erasure is not held for the ~30 s lease floor; sweep() finishes it later and an overdue job is reported
    (error_type physical_purge_overdue). physical="wait" keeps trying (PHYS_WAIT_S per attempt) until done or overdue."""
    t0 = time.time()
    j = _job(conn, jid)
    t, s = j["tenant"], j["subject"]
    rec = {"job_id": jid, "tenant": t, "subject": s, "done": False, "error_type": None, "steps": {}, "key_store": ks.describe()}
    if j["state"] == "done":                                # already complete: return the stored, store-written receipt
        v, sc, d = conn.execute("select verify, scope, extract(epoch from updated_at - created_at)::float8 from erasure_jobs where job_id=%s", (jid,)).fetchone()
        rec.update(done=True, verify=v, scope=json.loads(sc) if sc and sc.startswith("{") else sc, holds=_holds(conn, t, s),
                   delay_s=round(d, 3), stored_receipt=True)
        return rec
    ce = _claim(conn, jid, ttl_s)
    if ce is None:
        rec["error_type"] = "claimed_elsewhere"
        return rec
    hconn = db.connect(application_name="wp-erasure-hb")
    hconn.execute("set lock_timeout = '2s'")               # bounds one renewal call (call_max_s)
    hb = L.Heartbeat(lambda: _renew(hconn, jid, ce, ttl_s), ttl_s / 3, ttl_s, tolerate_transient=HB_TOLERATE, call_max_s=3.0)
    err = None
    try:
        with hb:
            if j["state"] == "logically_erased":            # item 4 follow-up: only the physical phase remains
                err = _physical(ks, verifier, jid, rec, physical, destroy_key, hb)
                return rec
            ts = time.time()
            conn.execute("select wp_erasure_mark(%s)", (jid,))
            rec["steps"]["mark_s"] = round(time.time() - ts, 3)
            hook("after_mark")
            if do_rescan:
                ts = time.time()
                sc, fo = rescan(conn, ks, jid)
                rec["steps"].update(rescan_s=round(time.time() - ts, 3), rescan_scanned=sc, rescan_found=fo)
            # Batches are sized to a TIME target (BATCH_TARGET_S), not only a row count: each batch is its own short
            # transaction, so it holds an xid (and every tenant's projection fence, see exp/xmin_stall.py) for at most
            # about that long, and stays far under the claim TTL and the role statement_timeout at any tenant size.
            ts, n, batches, per, size, conflicts = time.time(), 1, 0, [], min(batch, 500), 0
            while n:
                if hb.lost.is_set():
                    raise ClaimLost(jid)
                if cascade_deadline_s and time.time() - ts > cascade_deadline_s:
                    raise CascadeDeadline(jid)              # experiments only: the job stays open and resumable
                tb = time.time()
                try:
                    n = conn.execute("select wp_erasure_cascade(%s, %s)", (jid, size)).fetchone()[0]
                except psycopg.Error as e:
                    if getattr(e, "sqlstate", None) != "WPS23" or conflicts > 20:
                        raise
                    conflicts, n = conflicts + 1, 1            # the cursor moved under us (rescan rewind): rerun
                    continue
                dt = time.time() - tb
                per.append(round(dt, 3))
                batches += 1
                if dt > BATCH_TARGET_S:
                    size = max(50, size // 2)
                elif dt < BATCH_TARGET_S / 3:
                    size = min(batch, size * 2)
            rec["steps"].update(cascade_s=round(time.time() - ts, 3), cascade_batches=batches, cascade_batch_s=per,
                                cascade_batch_max_s=max(per) if per else 0, cascade_conflicts=conflicts, heartbeat_transient=hb.transient)
            wh = conn.execute("select event_id, speaker, hold_ids from erasure_withheld where job_id=%s order by event_id", (jid,)).fetchall()
            if wh:                                           # D1: covered by another subject's legal hold: queued, not dropped
                rec["withheld"] = [{"event_id": e, "speaker": sp, "hold_ids": h.split(",")} for e, sp, h in wh]
                rec["queued_until_release"] = True
                raise Withheld(jid)
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
                with conn.transaction():                        # the subject row stays locked: a hold cannot land mid-destroy
                    conn.execute("select wp_hold_check(%s, %s)", (t, s))
                    ks.destroy(t, s)                            # logical delete; the bytes go in the physical phase
                rec["steps"]["key_destroy_s"] = round(time.time() - ts, 3)
            _state(conn, jid, ce, "keys_destroyed")
            hook("before_complete")
            err = _physical(ks, verifier, jid, rec, physical, destroy_key, hb)
    except Exception as e:  # noqa: BLE001  (any failure leaves the job open with its type recorded)
        err = "legal_hold" if getattr(e, "sqlstate", None) == "WPS16" else ("withheld_by_hold" if isinstance(e, Withheld) else type(e).__name__)
        if err == "legal_hold":
            rec["held"] = True
            rec["holds"] = _holds(conn, t, s)
    finally:
        try:
            _release(conn, jid, ce, err)
        except psycopg.Error:
            pass
        hconn.close()
        rec["error_type"] = err
        rec["seconds"] = round(time.time() - t0, 3)
    return rec


def _physical(ks, verifier, jid, rec, physical, destroy_key, hb):
    """Key purge (coalesced: covers every key deleted before it) then the verifier; returns the error type or None."""
    v = verifier or Verifier()
    ts, attempts = time.time(), 0
    while True:
        attempts += 1
        if hb.lost.is_set():
            raise ClaimLost(jid)
        wait = PHYS_INLINE_S if physical == "followup" else PHYS_WAIT_S
        if destroy_key:
            rec["steps"]["key_purge"] = ks.purge(timeout_s=wait)
        tv = time.time()
        res = v.complete(jid, wait_s=wait)
        rec["steps"]["verify_s"] = round(time.time() - tv, 3)
        rec["steps"]["expunge"] = res.get("expunge")
        rec["steps"]["verify_timings"] = res.get("timings")
        rec.update(done=bool(res["done"]), receipt_state=res.get("receipt_state", "residue"), verify=res.get("verify"),
                   residue=res.get("residue"), scope=res.get("scope"), holds=res.get("holds"), delay_s=res.get("delay_s"),
                   logical_at=res.get("logical_at"), physical_deadline=res.get("physical_deadline"),
                   physical_pending=res.get("physical_pending"), logical_delay_s=res.get("logical_delay_s"))
        rec["steps"]["physical_attempts"], rec["steps"]["physical_s"] = attempts, round(time.time() - ts, 3)
        if res["done"]:
            return None
        if res.get("receipt_state") != "logically_erased_physical_purge_pending":
            return "residue"
        if res.get("overdue"):
            return "physical_purge_overdue"
        if physical == "followup":
            return None


def erase(conn, ks, es, tenant, subject_id, **kw):
    return run(conn, ks, es, request(conn, tenant, subject_id), **kw)


def place_hold(lconn, tenant, subject_id, hold_id, reason_code):
    """lconn: a legal-operations connection (db.legal()); the application role cannot place or release holds (D1)."""
    return lconn.execute("select wp_hold_place(%s::tenant_t, %s, %s, %s)", (tenant, subject_id, hold_id, reason_code)).fetchone()[0]


def release_hold(lconn, ks, es, hold_id, **kw):
    """Release a hold (legal-operations connection); queued erasures of that tenant then run (sweep, as the application).
    Returns the sweep result."""
    lconn.execute("select wp_hold_release(%s)", (hold_id,))
    t = lconn.execute("select tenant_id from legal_holds where hold_id=%s", (hold_id,)).fetchone()[0]
    return sweep(db.connect(application_name="wp-sweep"), ks, es, tenant=t, **kw)


def sweep(conn, ks, es, tenant=None, **kw):
    """Run every open job (of one tenant, or all) whose claim is free or expired. One job's failure never stops another."""
    out = {}
    for (jid,) in conn.execute("select job_id from erasure_jobs where state <> 'done' and (claim_expires is null or claim_expires < clock_timestamp()) "
                               "and (%s::text is null or tenant_id = %s) order by created_at", (tenant, tenant)).fetchall():
        try:
            out[jid] = run(conn, ks, es, jid, **kw)["done"]
        except Exception as e:  # noqa: BLE001
            out[jid] = f"error {type(e).__name__}"
    return out
