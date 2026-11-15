"""One long-open transaction (with an xid) in tenant A stalls the xmin-bounded outbox cursor of EVERY tenant. Documented limit.
Also shows reads of tenant B stay correct through the tail scan. usage: xmin_stall.py [hold_s=15]"""
import datetime as dt, sys, time
from common import *
from memsys import store, projector, reader, kms, chain
from memsys.db import iso

hold = int(sys.argv[1]) if len(sys.argv) > 1 else 15
conn, lock, es = conns(); db.reset_schema(conn); esx.install_templates(es)
for t in ("xa", "xb"): esx.drop_tenant(es, t)
TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
ev = lambda i, text: dict(ts_event=TS + dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=text)
store.append_events(conn, "xa", "s", [ev(0, "warm a")]); store.append_events(conn, "xb", "s", [ev(0, "warm b")])
p = projector.Projector(conn, lock, es, tenants=["xa", "xb"]); p.run()
holder = db.connect(autocommit=False)
enc = kms.get().enc("xa", "u1", "held event")
holder.execute("insert into sessions(tenant_id,session_id) values ('xa','held')")
h = chain.event_hash("0" * 64, "xa", "held", 1, "xa/held/1", iso(TS), "user", "user_typed", "message", "u1", enc)
holder.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) values ('xa','held',1,'xa/held/1',%s,'u1','user','user_typed','message',%s,%s,%s)", (TS, enc, "0" * 64, h))
t0 = time.time(); lag = []
rd = reader.Reader(conn, es)
tok = "stallprobe"
store.append_events(conn, "xb", "s", [ev(5, f"during stall {tok}")])
while time.time() - t0 < hold:
    p.run_once()
    n_pending = conn.execute("select count(*) from events e where tenant_id='xb' and (commit_xid, ingest_seq) > (select hwm_xid, hwm_seq from projection_state where view='events' and tenant_id='xb')").fetchone()[0]
    lag.append((round(time.time() - t0, 1), n_pending))
    time.sleep(1)
es_has = es.req("POST", "/sys-xb-events/_search", {"size": 0, "track_total_hits": True, "query": {"match": {"content": tok}}})[1]["hits"]["total"]["value"]
via = rd.search("xb", tok, k=5)["hits"]
holder.commit(); holder.close()
t1 = time.time(); p.run(); catch = round(time.time() - t1, 2)
es_has_after = es.req("POST", "/sys-xb-events/_search", {"size": 0, "track_total_hits": True, "query": {"match": {"content": tok}}})[1]["hits"]["total"]["value"]
out = {"hold_s": hold, "other_tenant_events_pending_during_hold": lag[-1][1], "es_docs_for_probe_during_hold": es_has, "reader_found_probe_via_tail_during_hold": bool(via and via[0]["via"] == "tail"),
       "es_docs_for_probe_after_commit": es_has_after, "catchup_s": catch, "lag_samples": lag[:5], "pass": es_has == 0 and es_has_after == 1 and bool(via)}
save("XMIN_stall", out); print(json.dumps(out, indent=1))
