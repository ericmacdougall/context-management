"""End-to-end smoke: append, derive, project, read, decide, erase. Not a proof; a plumbing check."""
import json

from exp.util import env, tenant, msg, fact, session, es_count_all
from wp import log, erasure, reader, projector, registry, db, config

conn, ks, es = env()
t = tenant("sm")
s = session(conn, t)
r = log.append(conn, ks, s, [msg(0, "hello zqsmokey", "Y"), msg(1, "X says zqsmokex", "X"), fact(2, "X", "Xavier", "diagnosis", "zqsmokeflu"),
                             fact(3, "Y", "Yolanda", "colour", "blue")])
print("append", {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
print("chain", log.verify_chain(conn, t, "s1", ks))
p = projector.Projector(conn, ks, es, t)
print("projector run", p.run(), p.stats)
rd = reader.Reader(conn, es, t)
print("search X", len(rd.search("zqsmokex")), "facts Xavier", len(rd.facts("Xavier")))
src = log.append(conn, ks, s, [msg(4, "approve scope EU", "X")])["accepted"][0]
ts = msg(5, "", "X")["ts_event"]
print("decide", log.decide(conn, ks, s, "X", "scope", "approve", None, {"text": "EU"}, src, "d1", ts))
print("decide dup", log.decide(conn, ks, s, "X", "scope", "approve", None, {"text": "EU"}, src, "d1", ts))
try:
    print(log.decide(conn, ks, s, "X", "scope", "approve", None, {"text": "US"}, src, "d2", ts))
except log.StaleDecision as e:
    print("stale decision refused:", str(e)[:80])
p.stop()
rec = erasure.erase(conn, ks, es, t, "X")
print("erase", json.dumps(rec, default=str)[:1500])
print("search X after", len(rd.search("zqsmokex")), "ES all X", es_count_all(es, t, {"term": {"subject_id": "X"}}))
print("p after erase", p.run(), p.stats, "search Y", len(rd.search("zqsmokey")))
print("unregistered", registry.unregistered(db.verifier(), db.verifier(db=config.KEYS_DB), es))
