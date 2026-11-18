"""Append path, inbox, analyst jobs and decisions. Every effect presents the caller's lease epoch; the store refuses a
stale one (schema.sql). Stale appends are kept as unadopted rows in `quarantine` (ciphertext), never in the chain."""
import hashlib
import hmac
import json
import uuid

import psycopg

from . import keys as K, mentions as M, secrets as SEC, sealer as SL
from .secrets import SecretDetected  # noqa: F401  (re-exported for callers)
from .sealer import SealUnavailable, SealError  # noqa: F401  (re-exported: the seal authority, DESIGN-NOTES A1 to A8)
from .mentions import WriteRefused, ContentTooLarge, MatcherBudgetExceeded, CallBudgetExceeded  # noqa: F401  (re-exported: m5 b1)
from .lease import is_stale_error, session_scope

ANALYST_KINDS = ("message",)        # kinds whose append also enqueues an analyst job (same transaction)


class StaleDecision(Exception):
    pass


class StaleWrite(Exception):
    pass


def _h(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def digest(content_enc):
    return hashlib.sha256(bytes(content_enc)).hexdigest()


def event_hash(prev, tenant, session, seq, eid, kind, subject, actor, trust, ts_iso, content_digest, epoch, item, action, based_on, source, mentions=(), scan=0,
               matcher="", key=None):
    """The chain covers the ciphertext DIGEST (so erasure can null the ciphertext and the chain still verifies), the
    mentions, the identifier-set version and (review C class 3) the matcher id the matcher ran under. With `key` (the tenant chain key from the key
    store, row 6) each link is an HMAC: whoever can write the events database but not read the key store cannot
    produce a link that verifies, however correctly chained. key=None is the unkeyed form (controls only)."""
    base = _h(prev, tenant, session, seq, eid, kind, subject, actor, trust, ts_iso, content_digest, epoch, item, action, based_on, source)
    h = _h(base, ",".join(mentions), scan) if (mentions or scan) else base
    h = _h(h, "matcher", matcher) if matcher else h
    return hmac.new(key, h.encode(), hashlib.sha256).hexdigest() if key is not None else h


def _noop(_name, _data=None):
    pass


class MentionsErased(Exception):
    """The content mentions a subject that has been erased (store SQLSTATE WPS18); the write is refused."""


class IdempotencyConflict(Exception):
    """The same idempotency key was presented with a different request (store SQLSTATE WPS09)."""


def _canonical(e):
    d = e.get("decision") or {}
    return json.dumps({"kind": e["kind"], "subject": e["subject_id"], "actor": e["actor"], "trust": e["trust_origin"],
                       "ts": e["ts_event"].isoformat(), "content": e["content"], "decision": d}, sort_keys=True)


SEAL_COLS = ("seal_key_id", "seal_mac", "seal_batch", "seal_index", "seal_count")


def append(conn, ks: K.KeyService, lease, events, hook=_noop, sealer=None):
    """events: dicts with subject_id, kind, actor, trust_origin, ts_event (aware datetime), content (str), optional
    idem_key and, for kind='decision', decision={item, action, based_on_event_id, source_event_id}.
    Returns {'accepted': [event_id], 'duplicates': [event_id], 'quarantined': n, ...}. Never raises on a stale epoch
    except SealUnavailable from the quarantine reseal (review J M2).
    Raises IdempotencyConflict when a key is reused with a different request, StaleDecision on a stale decision, and
    WriteRefused (ContentTooLarge WPM01, MatcherBudgetExceeded WPM02, CallBudgetExceeded WPM03; m5 b1) before anything
    else when an event is over the content size bound or its matcher key budget, or the call over its limits: the whole batch is refused, nothing is stored (no event, no
    quarantine row, no subject key) and the refusal is counted in write_refusals.
    The seal authority (`sealer`, else wp/sealer.py sealer_for; DESIGN-NOTES A8) admits, secret-scans, matches,
    encrypts and attests the batch; this function only orders, chains and inserts what it returned (the store refuses
    anything else: WPS40, WPS41). No authority reachable: SealUnavailable, nothing stored."""
    t, sid = lease.tenant, lease.scope.split(":", 1)[1]
    auth = sealer if sealer is not None else SL.sealer_for(conn, ks)
    eids = [uuid.uuid4().hex for _ in events]
    batch = uuid.uuid4().hex

    def seal(purpose="events"):
        try:
            got = auth.seal(t, lease.scope, lease.epoch, batch, [{"event_id": eid, "subject_id": e["subject_id"], "content": e["content"]}
                                                                for eid, e in zip(eids, events)], purpose=purpose)
        except WriteRefused as ex:
            raise _record(conn, t, ex) from None
        if [r["event_id"] for r in got] != eids:
            raise SealError("the seal authority returned another batch")
        return got

    sealed = seal()
    keys = ks.keys(t, sorted({e["subject_id"] for e in events}))       # created by the authority; needed for req_hash only
    prepared = []
    for e, eid, r in zip(events, eids, sealed):
        if keys.get(e["subject_id"]) is None:
            raise K.SubjectErased(e["subject_id"])
        d = e.get("decision") or {}
        prepared.append(dict(e, event_id=eid, idem_key=e.get("idem_key") or eid, req_hash=K.req_hmac(keys[e["subject_id"]], _canonical(e)),
                             item=ks.tenant_hmac(t, "item:" + d["item"]) if d else None, action=d.get("action"),
                             based_on=d.get("based_on_event_id"), source=d.get("source_event_id")))
    _apply_seal(prepared, sealed)
    ckey = ks.chain_key(t)
    hook("before_txn")
    for attempt in range(8):
        try:
            return _append_txn(conn, t, sid, lease, prepared, hook, ckey)
        except psycopg.Error as ex:
            st = getattr(ex, "sqlstate", None)
            if st == "WPS17" and attempt < 7:
                _apply_seal(prepared, seal())     # identifiers declared meanwhile: the authority rescans and re-attests
                continue
            if st == "23514" and (getattr(getattr(ex, "diag", None), "constraint_name", None) or "").endswith("_content_size"):
                raise _record(conn, t, ContentTooLarge("the store refused the ciphertext size (a path that skipped the check)")) from None
            if st == "WPS18":
                raise MentionsErased(str(ex).split("\n")[0]) from None
            if st in ("WPS15", "23505", "WPS14") and attempt < 7:
                continue                    # chain moved or a concurrent duplicate landed: re-read and retry
            if st == "WPS09":
                raise IdempotencyConflict(str(ex).split("\n")[0]) from None
            if is_stale_error(ex):
                # review J M2: an attestation is valid in one table only, so the authority seals the batch again for
                # quarantine (a fresh scan against the current identifier set; review J N1). A speaker erased meanwhile:
                # the whole batch is that speaker's or another's, so every event is dropped as erased.
                try:
                    _apply_seal(prepared, seal("quarantine"))
                except K.SubjectErased:
                    return {"accepted": [], "duplicates": [], "quarantined": 0, "dropped_over_cap": 0, "dropped_stale": 0,
                            "dropped_erased": len(prepared)}
                q = dropped = stale = 0
                for p in prepared:
                    try:
                        with conn.transaction():
                            n = conn.execute("insert into quarantine(tenant_id, session_id, subject_id, presented_epoch, reason, kind, content_enc, mentions, "
                                             "event_id, mention_scan_upto, mention_matcher, secret_scan, seal_key_id, seal_mac, seal_batch, seal_index, seal_count) "
                                             "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                             (t, sid, p["subject_id"], lease.epoch, "stale_epoch", p["kind"], p["content_enc"], p["mentions"],
                                              p["event_id"], p["scan"], p["matcher"], p["secret_scan"]) + tuple(p[c] for c in SEAL_COLS)).rowcount
                        q += n
                        dropped += 1 - n
                    except psycopg.Error as qe:
                        qs = getattr(qe, "sqlstate", None)
                        if qs in ("WPS17", "WPS18", "WPS42"):
                            # review J N1: a write rescanned below an identifier declared meanwhile, mentioning an erased
                            # subject, or removed by an erasure is refused by the store; it is dropped and counted here
                            # (the writer's lease was taken over: it is not retried)
                            stale += 1
                        elif qs != "WPS03":     # an erased subject's stale write is dropped, not kept
                            raise
                return {"accepted": [], "duplicates": [], "quarantined": q, "dropped_over_cap": dropped, "dropped_stale": stale,
                        "dropped_erased": len(prepared) - q - dropped - stale}
            if st in ("WPS05", "WPS04", "WPS13"):        # stale head, no valid user source, source already used
                raise StaleDecision(f"{st} " + str(ex).split("\n")[0]) from None
            raise


def _apply_seal(prepared, sealed):
    for p, r in zip(prepared, sealed):
        p.update(content_enc=r["content_enc"], content_digest=r["content_digest"], mentions=list(r["mentions"]), scan=r["scan"],
                 matcher=r["matcher"], secret_scan=r["secret_scan"], **{c: r[c] for c in SEAL_COLS})


def _record(conn, t, ex):
    """Count a refusal in write_refusals (code only) in its own transaction; never masks it: ex.recorded says whether
    the count landed. Returns ex for raising."""
    try:
        with conn.transaction():
            conn.execute("select wp_write_refused(%s::tenant_t, %s)", (t, ex.code))
        ex.recorded = True
    except Exception:                                # noqa: BLE001  (the refusal stands whatever counting does)
        ex.recorded = False
    return ex


def _admit(conn, t, check):
    try:
        check()
    except WriteRefused as ex:
        raise _record(conn, t, ex) from None


def _existing(conn, t, sid, prepared):
    return {k: (eid, h) for k, eid, h in conn.execute("select idem_key, event_id, req_hash from events where tenant_id=%s and session_id=%s and idem_key = any(%s)",
                                                       (t, sid, [p["idem_key"] for p in prepared])).fetchall()}


def _append_txn(conn, t, sid, lease, prepared, hook, ckey=None):
    with conn.transaction():
        conn.execute("select wp_subjects_ensure(%s::tenant_t, %s)", (t, sorted({p["subject_id"] for p in prepared} | {m for p in prepared for m in p["mentions"]})))
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
            h = event_hash(prev, t, sid, seq, p["event_id"], p["kind"], p["subject_id"], p["actor"], p["trust_origin"], ts, p["content_digest"], lease.epoch,
                           p["item"], p["action"], p["based_on"], p["source"], p["mentions"], p["scan"], p["matcher"], key=ckey)
            conn.execute(
                "insert into events(tenant_id,session_id,seq,event_id,idem_key,req_hash,kind,subject_id,actor,trust_origin,ts_event,content_enc,content_digest,"
                "mentions,mention_scan_upto,mention_matcher,prev_hash,hash,decision_item,decision_action,based_on_event_id,source_event_id,lease_scope,lease_epoch,"
                "secret_scan,seal_key_id,seal_mac,seal_batch,seal_index,seal_count) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s)",
                (t, sid, seq, p["event_id"], p["idem_key"], p["req_hash"], p["kind"], p["subject_id"], p["actor"], p["trust_origin"], p["ts_event"], p["content_enc"],
                 p["content_digest"], p["mentions"], p["scan"], p["matcher"], prev, h, p["item"], p["action"], p["based_on"], p["source"], lease.scope, lease.epoch,
                 p["secret_scan"]) + tuple(p[c] for c in SEAL_COLS))
            if p["kind"] in ANALYST_KINDS:
                conn.execute("insert into analyst_jobs(tenant_id, job_id, idem_key, session_id, input_event_ids, lease_scope, lease_epoch) values (%s,%s,%s,%s,%s,%s,%s)",
                             (t, uuid.uuid4().hex, "analyst:" + p["event_id"], sid, [p["event_id"]], lease.scope, lease.epoch))
            prev = h
            ids.append(p["event_id"])
        hook("before_commit")
        return {"accepted": ids, "duplicates": dups, "quarantined": 0}


def verify_chain(conn, tenant, session_id, ks=None, key=None, upto=None):
    """(ok, n, epochs): recompute every link under the tenant chain key (from `ks`); seqs must be 1..n with no gap or
    fork. Pass ks (normal) or key= (bytes; key=b"" is refused). An unkeyed check exists only as verify_chain_unkeyed."""
    if key is None:
        if ks is None:
            raise TypeError("verify_chain needs the key store: the chain is keyed (row 6)")
        key = ks.chain_key(tenant)
    return _verify(conn, tenant, session_id, key, upto)


def verify_chain_unkeyed(conn, tenant, session_id, upto=None):
    """The pre-row-6 check (plain SHA-256 links). Controls only: a forger with database access passes it."""
    return _verify(conn, tenant, session_id, None, upto)


def _verify(conn, tenant, session_id, key, upto):
    rows = conn.execute("select seq, event_id, kind, subject_id, actor, trust_origin, ts_event, content_digest, prev_hash, hash, lease_epoch, decision_item, "
                        "decision_action, based_on_event_id, source_event_id, content_enc, mentions, mention_scan_upto, mention_matcher "
                        "from events where tenant_id=%s and session_id=%s and (%s::bigint is null or seq <= %s) order by seq",
                        (tenant, session_id, upto, upto)).fetchall()
    prev, ok = "0" * 64, True
    for i, r in enumerate(rows):
        h = event_hash(prev, tenant, session_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6].isoformat(), r[7], r[10], r[11], r[12], r[13], r[14], r[16], r[17], r[18], key=key)
        intact = r[15] is None or digest(r[15]) == r[7]      # present content must match its digest
        ok = ok and r[0] == i + 1 and r[8] == prev and r[9] == h and intact
        prev = r[9]
    return ok and _verify_nulls(conn, tenant, rows), len(rows), sorted({r[10] for r in rows})


def _verify_nulls(conn, tenant, rows):
    """Review C class 5 (F7): NULL content verifies only with a tombstone whose erasure job still exists for the same
    tenant and subject and got past 'requested' (only wp_erasure_cascade writes tombstones, after the mark). Deleting
    the tombstone, its job, or pointing it at another subject's job makes verification fail. (Decision R1 removed span
    redaction, so there is no other legitimate NULL; a present ciphertext is checked against its digest in _verify.)"""
    ids = sorted({r[1] for r in rows if r[15] is None})
    if not ids:
        return True
    ok = {x[0] for x in conn.execute(
        "select tb.event_id from tombstones tb join erasure_jobs j on j.job_id = tb.job_id and j.tenant_id = tb.tenant_id "
        "and j.subject_id = tb.subject_id where tb.tenant_id = %s and tb.event_id = any(%s) and j.state <> 'requested'",
        (tenant, ids)).fetchall()}
    return set(ids) <= ok


# ---------------------------------------------------------------- inbox
def inbox_enqueue(conn, ks, lease, item_id, target_session, subject_id, payload):
    _admit(conn, lease.tenant, lambda: M.check_size(json.dumps(payload), 0, "inbox payload"))      # m5 b1 B3: WPM01
    SEC.check([json.dumps(payload)], "inbox")
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
    store refuses it if any input was erased meanwhile. A result over the size bound raises ContentTooLarge (WPM01)."""
    _admit(conn, tenant, lambda: M.check_size(result_text, 0, "job result"))
    SEC.check([result_text], "job_result")
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
def decide(conn, ks, lease, subject_id, item, action, based_on_event_id, payload, source_event_id, idem_key, ts, sealer=None):
    """A decision is a typed event bound by the store to the item head it was made against (based_on_event_id must be
    the head's last event; 'execute' additionally requires that event to be the current approval), to exactly one
    typed user message in the same session (a decision is never a source), and to its idempotency key.
    Returns ('accepted'|'duplicate'|'quarantined', event_id); raises StaleDecision when the store refuses it."""
    body = json.dumps({"item": item, "action": action, "payload": payload, "based_on_event_id": based_on_event_id}, sort_keys=True)
    r = append(conn, ks, lease, [dict(subject_id=subject_id, kind="decision", actor="user", trust_origin="user_typed", ts_event=ts, content=body,
                                      idem_key=idem_key, decision={"item": item, "action": action, "based_on_event_id": based_on_event_id,
                                                                   "source_event_id": source_event_id})], sealer=sealer)
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
