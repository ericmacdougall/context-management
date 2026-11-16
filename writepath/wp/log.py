"""Append path, inbox, analyst jobs and decisions. Every effect presents the caller's lease epoch; the store refuses a
stale one (schema.sql). Stale appends are kept as unadopted rows in `quarantine` (ciphertext), never in the chain."""
import hashlib
import json
import uuid

import psycopg

from . import keys as K
from .lease import is_stale_error, session_scope

ANALYST_KINDS = ("message",)        # kinds whose append also enqueues an analyst job (same transaction)


class StaleDecision(Exception):
    pass


class StaleWrite(Exception):
    pass


def _h(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def event_hash(prev, tenant, session, seq, eid, kind, subject, actor, trust, ts_iso, content_enc, epoch, item, action, based_on, source):
    return _h(prev, tenant, session, seq, eid, kind, subject, actor, trust, ts_iso, hashlib.sha256(bytes(content_enc)).hexdigest(), epoch, item, action, based_on, source)


def _noop(_name, _data=None):
    pass


class IdempotencyConflict(Exception):
    """The same idempotency key was presented with a different request (store SQLSTATE WPS09)."""


def _canonical(e):
    d = e.get("decision") or {}
    return json.dumps({"kind": e["kind"], "subject": e["subject_id"], "actor": e["actor"], "trust": e["trust_origin"],
                       "ts": e["ts_event"].isoformat(), "content": e["content"], "decision": d}, sort_keys=True)


def append(conn, ks: K.KeyService, lease, events, hook=_noop):
    """events: dicts with subject_id, kind, actor, trust_origin, ts_event (aware datetime), content (str), optional
    idem_key and, for kind='decision', decision={item, action, based_on_event_id, source_event_id}.
    Returns {'accepted': [event_id], 'duplicates': [event_id], 'quarantined': n, ...}. Never raises on a stale epoch.
    Raises IdempotencyConflict when a key is reused with a different request, StaleDecision on a stale decision."""
    t, sid = lease.tenant, lease.scope.split(":", 1)[1]
    prepared = []
    keys = ks.keys(t, [e["subject_id"] for e in events], create=True)
    for e in events:
        eid = uuid.uuid4().hex
        d = e.get("decision") or {}
        prepared.append(dict(e, event_id=eid, idem_key=e.get("idem_key") or eid,
                             req_hash=K.req_hmac(keys[e["subject_id"]], _canonical(e)),
                             content_enc=K.seal(keys[e["subject_id"]], t, e["subject_id"], eid, e["content"]),
                             item=ks.tenant_hmac(t, "item:" + d["item"]) if d else None, action=d.get("action"),
                             based_on=d.get("based_on_event_id"), source=d.get("source_event_id")))
    hook("before_txn")
    for attempt in range(8):
        try:
            return _append_txn(conn, t, sid, lease, prepared, hook)
        except psycopg.Error as ex:
            st = getattr(ex, "sqlstate", None)
            if st in ("WPS15", "23505", "WPS14") and attempt < 7:
                continue                    # chain moved or a concurrent duplicate landed: re-read and retry
            if st == "WPS09":
                raise IdempotencyConflict(str(ex).split("\n")[0]) from None
            if is_stale_error(ex):
                q = dropped = 0
                for p in prepared:
                    try:
                        with conn.transaction():
                            n = conn.execute("insert into quarantine(tenant_id, session_id, subject_id, presented_epoch, reason, kind, content_enc) values (%s,%s,%s,%s,%s,%s,%s)",
                                             (t, sid, p["subject_id"], lease.epoch, "stale_epoch", p["kind"], p["content_enc"])).rowcount
                        q += n
                        dropped += 1 - n
                    except psycopg.Error as qe:
                        if getattr(qe, "sqlstate", None) != "WPS03":     # an erased subject's stale write is dropped, not kept
                            raise
                return {"accepted": [], "duplicates": [], "quarantined": q, "dropped_over_cap": dropped, "dropped_erased": len(prepared) - q - dropped}
            if st in ("WPS05", "WPS04", "WPS13"):        # stale head, no valid user source, source already used
                raise StaleDecision(f"{st} " + str(ex).split("\n")[0]) from None
            raise


def _existing(conn, t, sid, prepared):
    return {k: (eid, h) for k, eid, h in conn.execute("select idem_key, event_id, req_hash from events where tenant_id=%s and session_id=%s and idem_key = any(%s)",
                                                       (t, sid, [p["idem_key"] for p in prepared])).fetchall()}


def _append_txn(conn, t, sid, lease, prepared, hook):
    with conn.transaction():
        conn.execute("select wp_subjects_ensure(%s::tenant_t, %s)", (t, sorted({p["subject_id"] for p in prepared})))
        have = _existing(conn, t, sid, prepared)
        for p in prepared:
            if p["idem_key"] in have and have[p["idem_key"]][1] != p["req_hash"]:
                raise IdempotencyConflict(p["idem_key"])
        todo = [p for p in prepared if p["idem_key"] not in have]
        dups = [have[p["idem_key"]][0] for p in prepared if p["idem_key"] in have]
        if not todo:
            return {"accepted": [], "duplicates": dups, "quarantined": 0}
        hook("after_precheck")
        r = conn.execute("select next_seq, last_hash from chains where tenant_id=%s and session_id=%s", (t, sid)).fetchone()
        next_seq, prev = r if r else (1, "0" * 64)
        hook("in_txn")
        ids = []
        for i, p in enumerate(todo):
            seq = next_seq + i
            ts = p["ts_event"].isoformat()
            h = event_hash(prev, t, sid, seq, p["event_id"], p["kind"], p["subject_id"], p["actor"], p["trust_origin"], ts, p["content_enc"], lease.epoch,
                           p["item"], p["action"], p["based_on"], p["source"])
            conn.execute(
                "insert into events(tenant_id,session_id,seq,event_id,idem_key,req_hash,kind,subject_id,actor,trust_origin,ts_event,content_enc,prev_hash,hash,"
                "decision_item,decision_action,based_on_event_id,source_event_id,lease_scope,lease_epoch) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (t, sid, seq, p["event_id"], p["idem_key"], p["req_hash"], p["kind"], p["subject_id"], p["actor"], p["trust_origin"], p["ts_event"], p["content_enc"], prev, h,
                 p["item"], p["action"], p["based_on"], p["source"], lease.scope, lease.epoch))
            if p["kind"] in ANALYST_KINDS:
                conn.execute("insert into analyst_jobs(tenant_id, job_id, idem_key, session_id, input_event_ids, lease_scope, lease_epoch) values (%s,%s,%s,%s,%s,%s,%s)",
                             (t, uuid.uuid4().hex, "analyst:" + p["event_id"], sid, [p["event_id"]], lease.scope, lease.epoch))
            prev = h
            ids.append(p["event_id"])
        hook("before_commit")
        return {"accepted": ids, "duplicates": dups, "quarantined": 0}


def verify_chain(conn, tenant, session_id):
    """(ok, n, epochs): recompute every hash; seqs must be 1..n with no gap or fork."""
    rows = conn.execute("select seq, event_id, kind, subject_id, actor, trust_origin, ts_event, content_enc, prev_hash, hash, lease_epoch, decision_item, "
                        "decision_action, based_on_event_id, source_event_id from events where tenant_id=%s and session_id=%s order by seq", (tenant, session_id)).fetchall()
    prev, ok = "0" * 64, True
    for i, r in enumerate(rows):
        h = event_hash(prev, tenant, session_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6].isoformat(), r[7], r[10], r[11], r[12], r[13], r[14])
        ok = ok and r[0] == i + 1 and r[8] == prev and r[9] == h
        prev = r[9]
    return ok, len(rows), sorted({r[10] for r in rows})


# ---------------------------------------------------------------- inbox
def inbox_enqueue(conn, ks, lease, item_id, target_session, subject_id, payload):
    k = ks.keys(lease.tenant, [subject_id], create=True)[subject_id]
    enc = K.seal(k, lease.tenant, subject_id, "inbox:" + item_id, json.dumps(payload))
    with conn.transaction():
        conn.execute("select wp_subjects_ensure(%s::tenant_t, %s)", (lease.tenant, [subject_id]))
        conn.execute("insert into inbox(tenant_id,item_id,sender_session,target_session,subject_id,payload_enc,lease_scope,lease_epoch) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (lease.tenant, item_id, lease.scope.split(":", 1)[1], target_session, subject_id, enc, lease.scope, lease.epoch))


def _fenced(conn, sql, args):
    try:
        with conn.transaction():
            return conn.execute(sql, args).fetchone()
    except psycopg.Error as ex:
        if is_stale_error(ex):
            raise StaleWrite(str(ex).split("\n")[0]) from None
        raise


def inbox_claim(conn, lease, item_id):
    """Deliver an item to the lease holder's session. Returns True if delivered now."""
    sid = lease.scope.split(":", 1)[1]
    r = _fenced(conn, "update inbox set state='claimed', claimed_epoch=%s, shown_count=shown_count+1, lease_scope=%s, lease_epoch=%s "
                      "where tenant_id=%s and item_id=%s and target_session=%s and (state='new' or (state='claimed' and claimed_epoch < %s)) returning 1",
                (lease.epoch, lease.scope, lease.epoch, lease.tenant, item_id, sid, lease.epoch))
    return bool(r)


def inbox_confirm(conn, lease, item_id):
    r = _fenced(conn, "update inbox set state='confirmed', confirmed_epoch=%s, lease_scope=%s, lease_epoch=%s "
                      "where tenant_id=%s and item_id=%s and state='claimed' and claimed_epoch=%s returning 1",
                (lease.epoch, lease.scope, lease.epoch, lease.tenant, item_id, lease.epoch))
    return bool(r)


# ---------------------------------------------------------------- analyst jobs
def job_claim(conn, tenant, ttl_s=30.0):
    """Claim the oldest queued (or expired) job. Returns (job_id, claim_epoch, input_event_ids) or None."""
    with conn.transaction():
        r = conn.execute("select job_id from analyst_jobs where tenant_id=%s and (state='queued' or (state='claimed' and claim_expires < clock_timestamp())) "
                         "order by job_id limit 1 for update skip locked", (tenant,)).fetchone()
        if not r:
            return None
        return conn.execute("update analyst_jobs set state='claimed', claim_epoch=claim_epoch+1, claim_expires=clock_timestamp()+make_interval(secs => %s) "
                            "where tenant_id=%s and job_id=%s returning job_id, claim_epoch, input_event_ids", (ttl_s, tenant, r[0])).fetchone()


def job_complete(conn, ks, tenant, job_id, claim_epoch, result_text, verify):
    """Completion is a conditional write: current claimant only (claim_epoch), verify result mandatory and ok, and the
    store refuses it if any input was erased meanwhile."""
    subj = conn.execute("select e.subject_id from analyst_jobs j join events e on e.tenant_id=j.tenant_id and e.event_id=j.input_event_ids[1] "
                        "where j.tenant_id=%s and j.job_id=%s", (tenant, job_id)).fetchone()
    k = ks.keys(tenant, [subj[0]])[subj[0]] if subj else None
    enc = K.seal(k, tenant, subj[0], "job:" + job_id, result_text) if k else None
    try:
        with conn.transaction():
            r = conn.execute("update analyst_jobs set state='done', result_enc=%s, verify=%s where tenant_id=%s and job_id=%s and claim_epoch=%s and state='claimed' returning 1",
                             (enc, json.dumps(verify), tenant, job_id, claim_epoch)).fetchone()
        return bool(r)
    except psycopg.Error as ex:
        if getattr(ex, "sqlstate", None) in ("WPS01", "WPS03", "23514"):
            return False
        raise


# ---------------------------------------------------------------- decisions
def decide(conn, ks, lease, subject_id, item, action, based_on_event_id, payload, source_event_id, idem_key, ts):
    """A decision is a typed event bound by the store to the item head it was made against (based_on_event_id must be
    the head's last event; 'execute' additionally requires that event to be the current approval), to exactly one
    typed user message in the same session (a decision is never a source), and to its idempotency key.
    Returns ('accepted'|'duplicate'|'quarantined', event_id); raises StaleDecision when the store refuses it."""
    body = json.dumps({"item": item, "action": action, "payload": payload, "based_on_event_id": based_on_event_id}, sort_keys=True)
    r = append(conn, ks, lease, [dict(subject_id=subject_id, kind="decision", actor="user", trust_origin="user_typed", ts_event=ts, content=body,
                                      idem_key=idem_key, decision={"item": item, "action": action, "based_on_event_id": based_on_event_id,
                                                                   "source_event_id": source_event_id})])
    if r["accepted"]:
        return "accepted", r["accepted"][0]
    if r["duplicates"]:
        return "duplicate", r["duplicates"][0]
    return "quarantined", None


def head(conn, ks, tenant, item):
    """(version, last_event_id, last_action) of the tenant-wide item head."""
    r = conn.execute("select version, last_event_id, last_action from decision_heads where tenant_id=%s and item=%s",
                     (tenant, ks.tenant_hmac(tenant, "item:" + item))).fetchone()
    return r or (0, None, None)
