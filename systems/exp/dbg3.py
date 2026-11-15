import sys, random, datetime as dt
sys.path.insert(0, ".")
from exp import c5_attack as c
from memsys import db, store, derive, kms
conn = db.connect(autocommit=True); db.reset_schema(conn)
rng = random.Random(5); evs, expect = c.gen_case(rng)
ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
for i, e in enumerate(evs):
    try: store.append_events(conn, "c5e", "s0", [dict(ts_event=ts + dt.timedelta(seconds=i), actor=e["actor"], trust_origin=e["trust_origin"], kind="m", subject_id="w0", content=e["text"])])
    except Exception as ex: print("rej", e["text"], type(ex).__name__)
n = derive.derive_batch(conn, "c5e", 1, limit=500, touch=False); print("derived", n)
print(conn.execute("select count(*), min(status) from facts").fetchall(), conn.execute("select hwm_xid from projection_state").fetchall(), conn.execute("select max(commit_xid) from events").fetchall())
print(expect, [(r[0], r[1]) for r in conn.execute("select status, trust_origin from facts").fetchall()])
