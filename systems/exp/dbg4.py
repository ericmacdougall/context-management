import sys, time, datetime as dt
sys.path.insert(0, "exp")
from common import *
from memsys import store, derive, conceptq, kms, erasure
conn, lock, es = conns(); db.reset_schema(conn)
T="rc2"; TS = dt.datetime(2026,3,1,tzinfo=dt.timezone.utc)
ev = lambda i, text, s: dict(ts_event=TS+dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind="m", subject_id=s, content=text)
store.append_events(conn,T,"s1",[ev(10,"EntX's city is zm","vic")]); store.append_events(conn,T,"k1",[ev(11,"EntX's pet is cat","keeper")])
while derive.derive_batch(conn,T,1,touch=True): pass
h = kms.get().hmac_many(T,["EntX"])[0]
print(conn.execute("select concept_id=%s, dirty, next_fold_at<=now() from concepts",(h,)).fetchall())
conn.execute("update concepts set next_fold_at=clock_timestamp() where tenant_id=%s and concept_id=%s",(T,h))
cl=[c for c in conceptq.claim(conn,limit=100000) if c[1]==h]; print("claim",len(cl))
tn,cid,tok,gen=cl[0]; res=conceptq.compute_fold(conn.cursor(),tn,cid); print("fold evs",res[1])
erasure.phase1(conn,kms.get(),T,"vic","rj")
print(conceptq.commit_fold(conn,tn,cid,tok,gen,res,fence=False))
print(conn.execute("select input_event_ids from concepts").fetchall())
