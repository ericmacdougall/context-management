"""Smoke test: schema, encryption, hash chain, append-only trigger, key destruction."""
import datetime as dt
import sys
sys.path.insert(0, ".")
from memsys import db, store, chain

conn = db.connect()
db.reset_schema(conn)
conn.autocommit = False
ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
ev = lambda i, c: dict(ts_event=ts + dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=c)
ids, w = store.append_events(conn, "t01", "s1", [ev(0, "Alice's city is Paris"), ev(1, "hello")])
ids2, w = store.append_events(conn, "t01", "s1", [ev(2, "Bob's age is 7")])
print(ids, ids2)
rows = list(store.session_rows(conn, "t01", "s1"))
print("chain", chain.verify_chain(rows))
print(conn.execute("select mem_dec(tenant_id, subject_id, content_enc) from events order by seq").fetchall())
try:
    conn.execute("update events set kind='x'")
except Exception as e:
    print("update blocked:", str(e)[:50]); conn.rollback()
conn.execute("update keys set key=null, destroyed_at=now()"); conn.commit()
print(conn.execute("select mem_dec(tenant_id, subject_id, content_enc) from events order by seq").fetchall())
print("chain after shred", chain.verify_chain(list(store.session_rows(conn, "t01", "s1"))))
try:
    store.append_events(conn, "t01", "s1", [ev(3, "late")])
except Exception as e:
    print("append after erasure blocked:", str(e)[:60]); conn.rollback()
print(conn.execute("select next_seq from sessions").fetchall())
