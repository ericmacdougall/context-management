"""Append path for the event log."""
import time

from . import chain, kms as _kms
from .db import iso, retry_deadlock
from .kms import SubjectErased


def append_events(conn, tenant_id, session_id, events, kms=None):
    """Append events to one session in a single transaction.

    events: dicts with ts_event (aware datetime), actor, trust_origin, kind, subject_id, content (plaintext) and
    optionally client_event_id (idempotency key: a retry after a lost commit response returns the already-stored ids).
    Encryption happens in the key service BEFORE the transaction; the transaction takes FOR SHARE on each subject's
    fence row and refuses an erased subject, so an append racing an erasure either lands before it (and is erased
    with it) or fails. Returns (event_ids, lock_wait_seconds)."""
    kms = kms or _kms.get()
    todo = events
    cids = [e.get("client_event_id") for e in events]
    if any(cids):                     # already stored under a lost response? skip re-encrypting those
        have = {r[0]: r[1] for r in conn.execute("select client_event_id, event_id from events where tenant_id=%s and client_event_id = any(%s)",
                                                 (tenant_id, [c for c in cids if c])).fetchall()}
        if have and all(c in have for c in cids if c) and all(c for c in cids):
            return [have[c] for c in cids], 0.0
    enc = kms.enc_many(tenant_id, [e["subject_id"] for e in events], [e["content"] for e in events])

    def txn():
        with conn.transaction():
            cur = conn.cursor()
            for s in sorted({e["subject_id"] for e in events}):
                cur.execute("insert into subjects(tenant_id, subject_id) values (%s,%s) on conflict do nothing", (tenant_id, s))
                if cur.execute("select erased from subjects where tenant_id=%s and subject_id=%s for share", (tenant_id, s)).fetchone()[0]:
                    raise SubjectErased(s)
            cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing", (tenant_id, session_id))
            t0 = time.perf_counter()
            next_seq, last_hash = cur.execute(
                "select next_seq, last_hash from sessions where tenant_id=%s and session_id=%s for update", (tenant_id, session_id)).fetchone()
            wait = time.perf_counter() - t0
            if any(cids):       # idempotency re-check under the session lock
                have = {r[0]: r[1] for r in cur.execute("select client_event_id, event_id from events where tenant_id=%s and client_event_id = any(%s)",
                                                        (tenant_id, [c for c in cids if c])).fetchall()}
                if have and all(c in have for c in cids):
                    return [have[c] for c in cids], wait
            rows, ids, prev = [], [], last_hash
            for i, e in enumerate(events):
                seq = next_seq + i
                eid = f"{tenant_id}/{session_id}/{seq}"
                h = chain.event_hash(prev, tenant_id, session_id, seq, eid, iso(e["ts_event"]), e["actor"], e["trust_origin"], e["kind"], e["subject_id"], enc[i])
                rows.append((tenant_id, session_id, seq, eid, cids[i], e["ts_event"], e["subject_id"], e["actor"], e["trust_origin"], e["kind"], enc[i], prev, h))
                ids.append(eid)
                prev = h
            cur.executemany(
                "insert into events(tenant_id,session_id,seq,event_id,client_event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            cur.execute("update sessions set next_seq=%s, last_hash=%s where tenant_id=%s and session_id=%s", (next_seq + len(events), prev, tenant_id, session_id))
            return ids, wait
    return retry_deadlock(txn)


def session_rows(conn, tenant_id, session_id):
    cur = conn.cursor()
    cur.execute("select tenant_id,session_id,seq,event_id,ts_event,actor,trust_origin,kind,subject_id,content_enc,"
                "prev_hash,hash from events where tenant_id=%s and session_id=%s order by seq", (tenant_id, session_id))
    cols = [d.name for d in cur.description]
    for r in cur:
        d = dict(zip(cols, r))
        d["ts_event"] = iso(d["ts_event"])
        d["content_enc"] = bytes(d["content_enc"])
        yield d


def read_plain(conn, tenant_id, where_sql, args, kms=None):
    """Decrypt events app-side. Returns rows as dicts (without ciphertext) with `plain` (None if the key is gone)."""
    kms = kms or _kms.get()
    cur = conn.cursor()
    cur.execute("select tenant_id,session_id,seq,event_id,ts_event,actor,trust_origin,kind,subject_id,hash,commit_xid,ingest_seq,content_enc,"
                "exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) as tomb "
                f"from events e where tenant_id=%s and {where_sql}", (tenant_id,) + tuple(args))
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    pl = kms.dec_many(tenant_id, [(r["subject_id"], r["content_enc"]) for r in rows])
    for r, p in zip(rows, pl):
        r["plain"] = p
        del r["content_enc"]
    return rows
