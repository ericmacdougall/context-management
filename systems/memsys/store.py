"""Append path for the event log."""
import time

from . import chain
from .db import iso


def append_events(conn, tenant_id, session_id, events):
    """Append events to one session in a single transaction.

    events: dicts with ts_event (aware datetime), actor, trust_origin, kind,
    subject_id, content (plaintext). The per-session sequence and hash chain are
    assigned under a row lock on an existing `sessions` row. Returns
    (event_ids, lock_wait_seconds). Raises psycopg errors unchanged (the caller's
    transaction rolls back; seq is never consumed by a failed append).
    """
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing",
                    (tenant_id, session_id))
        t0 = time.perf_counter()
        next_seq, last_hash = cur.execute(
            "select next_seq, last_hash from sessions where tenant_id=%s and session_id=%s for update",
            (tenant_id, session_id)).fetchone()
        wait = time.perf_counter() - t0
        enc = [r[0] for r in cur.execute(
            "select mem_enc(%s, s, c) from unnest(%s::text[], %s::text[]) with ordinality as u(s, c, n) order by n",
            (tenant_id, [e["subject_id"] for e in events], [e["content"] for e in events])).fetchall()]
        rows, ids = [], []
        prev = last_hash
        for i, e in enumerate(events):
            seq = next_seq + i
            eid = f"{tenant_id}/{session_id}/{seq}"
            ts = iso(e["ts_event"])
            h = chain.event_hash(prev, tenant_id, session_id, seq, eid, ts, e["actor"], e["trust_origin"],
                                 e["kind"], e["subject_id"], enc[i])
            rows.append((tenant_id, session_id, seq, eid, e["ts_event"], e["subject_id"], e["actor"],
                         e["trust_origin"], e["kind"], enc[i], prev, h))
            ids.append(eid)
            prev = h
        cur.executemany(
            "insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,"
            "content_enc,prev_hash,hash) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        cur.execute("update sessions set next_seq=%s, last_hash=%s where tenant_id=%s and session_id=%s",
                    (next_seq + len(events), prev, tenant_id, session_id))
    return ids, wait


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
