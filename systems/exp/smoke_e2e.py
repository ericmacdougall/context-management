"""Tiny end-to-end run: 2 tenants, ~1.5k events. Not an experiment; validates plumbing."""
import time
from common import *
from memsys import gen, store, derive, projector, reader, erasure, chain

conn, lock, es = conns()
db.reset_schema(conn)
esx.install_templates(es)
for t in ("t01", "t02"):
    esx.drop_tenant(es, t)
    sess = gen.gen_tenant(t, 1, 20, 40)
    t0 = time.time(); n = gen.load(conn, t, sess, 1, store); print(t, "loaded", n, round(time.time() - t0, 1), "s")
p = projector.Projector(conn, lock, es, recipes=(1,), live_recipe=1, batch=300)
t0 = time.time(); p.run(); print("projected", round(time.time() - t0, 1), "s")
print("events", compare_events(conn, es, ["t01", "t02"]))
print("facts", compare_facts(conn, es, ["t01", "t02"], 1))
print(conn.execute("select status, count(*) from facts group by 1 order by 1").fetchall())
r = reader.Reader(conn, es)
res = r.search("t01", "Alice favorite color blue")
print(res["hits"][:3], res["mark"], res["tail"])
print(r.query_facts("t01", "Alice", "city"))
rc = erasure.erase_subject(conn, lock, es, "t01", "u3")
print(json.dumps(rc, indent=1, default=str)[:1500])
print("events after erase", compare_events(conn, es, ["t01", "t02"]))
print("facts after erase", compare_facts(conn, es, ["t01", "t02"], 1))
