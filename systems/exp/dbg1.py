from common import *
from memsys.db import iso
conn, lock, es = conns()
t="t02"
exp = pg_expected_events(conn, t)
idx,i,src = next(scan(es, esx.ev_ralias(t), True))
print(i, src.keys())
cur = conn.cursor()
cur.execute("select session_id, seq, event_id, ts_event, actor, trust_origin, kind, subject_id, mem_dec(tenant_id, subject_id, content_enc) from events where event_id=%s",(i,))
r = cur.fetchone()
d = esx.event_doc(t, r[0], r[1], r[2], iso(r[3]), *r[4:])
for k in d:
    if d[k] != src.get(k): print("DIFF", k, repr(d[k])[:80], repr(src.get(k))[:80])
print(esx.doc_hash(d), esx.doc_hash(src), exp[i])
