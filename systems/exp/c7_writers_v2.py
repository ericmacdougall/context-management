"""C7: N writers appending single events to ONE session. usage: c7_writers.py [total=100000] [writers=16]"""
import datetime as dt, multiprocessing as mp, sys, time
from common import *
from memsys import store, chain

TS = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)

def worker(args):
    w, n, tenant, session, lock_mode = args
    conn = db.connect(autocommit=True)
    waits, lat, errs = [], [], 0
    for i in range(n):
        e = dict(ts_event=TS + dt.timedelta(milliseconds=w * 1000 + i), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=f"w{w}-n{i}")
        t0 = time.perf_counter()
        if lock_mode == "row_lock":
            _, wait = store.append_events(conn, tenant, session, [e])
            waits.append(wait)
        else:   # NO-LOCK CONTROL: seq = max(seq)+1, no row lock
            try:
                nolock_append(conn, tenant, session, e, lock_mode)
            except Exception:
                errs += 1
        lat.append(time.perf_counter() - t0)
    return waits, lat, errs

def nolock_append(conn, tenant, session, e, mode):
    """CONTROLS without the session row lock. mode 'max': seq=max(seq)+1 (unique violations); mode 'seq': seq from a sequence and
    prev hash = latest visible hash (no unique violation, so rows commit with a BROKEN chain)."""
    from memsys import kms
    enc = kms.get().enc(tenant, e["subject_id"], e["content"])
    with conn.transaction():
        cur = conn.cursor()
        if mode == "seq":
            seq = cur.execute("select nextval('c7ctl_seq')").fetchone()[0]
            r = cur.execute("select hash from events where tenant_id=%s and session_id=%s order by seq desc limit 1", (tenant, session)).fetchone()
            prev = r[0] if r else "0" * 64
        else:
            seq, prev = cur.execute("select coalesce(max(seq),0)+1, coalesce((select hash from events where tenant_id=%s and session_id=%s order by seq desc limit 1), repeat('0',64)) from events where tenant_id=%s and session_id=%s", (tenant, session, tenant, session)).fetchone()
        eid = f"{tenant}/{session}/{seq}"
        h = chain.event_hash(prev, tenant, session, seq, eid, db.iso(e["ts_event"]), e["actor"], e["trust_origin"], e["kind"], e["subject_id"], enc)
        cur.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tenant, session, seq, eid, e["ts_event"], e["subject_id"], e["actor"], e["trust_origin"], e["kind"], enc, prev, h))

def verify(conn, tenant, session, expected):
    rows = list(store.session_rows(conn, tenant, session))
    ok, bad, n = chain.verify_chain(rows)
    seqs = [r["seq"] for r in rows]
    from memsys import kms
    rr = conn.execute("select subject_id, content_enc from events where tenant_id=%s and session_id=%s", (tenant, session)).fetchall()
    payload = kms.get().dec_many(tenant, [(a, b) for a, b in rr])
    return {"rows": n, "chain_ok": ok, "first_bad_seq": bad, "gapless": seqs == list(range(1, len(seqs) + 1)),
            "distinct_payloads": len(set(payload)), "duplicated_payloads": len(payload) - len(set(payload)),
            "lost_payloads": len(expected - set(payload)), "unexpected_payloads": len(set(payload) - expected)}

def run(total, writers, mode, session):
    per0 = total // writers
    conn = db.connect(autocommit=True)
    per = total // writers
    expected = {f"w{w}-n{i}" for w in range(writers) for i in range(per)}
    t0 = time.time()
    with mp.get_context("fork").Pool(writers) as p:
        res = p.map(worker, [(w, per, "c7", session, mode) for w in range(writers)])
    dur = time.time() - t0
    waits = [x for r in res for x in r[0]]; lat = [x for r in res for x in r[1]]
    out = {"mode": mode, "writers": writers, "appends_attempted": per * writers, "duration_s": round(dur, 1),
           "throughput_per_s": round(per * writers / dur, 1), "append_latency_s": percentiles(lat),
           "lock_wait_s": percentiles(waits), "errors": sum(r[2] for r in res)}
    out["verify"] = verify(conn, "c7", session, expected)
    return out

if __name__ == "__main__":
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
    writers = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    conn = db.connect(autocommit=True)
    db.reset_schema(conn)
    out = {"workload": f"{writers} processes, 1 event per transaction, one session, synchronous_commit=on, PG16 container 4 cpus/4g on t2-dev (shared 48-core host)",
           "disk_free_gb_before": disk_free_gb()}
    out["row_lock"] = run(total, writers, "row_lock", "S")
    print(json.dumps(out["row_lock"]), flush=True)
    conn.execute("create sequence if not exists c7ctl_seq")
    out["no_lock_control_max_plus_one"] = run(4000, writers, "max", "S2")
    out["no_lock_control_sequence"] = run(4000, writers, "seq", "S3")
    print(json.dumps(out["no_lock_control_max_plus_one"]), json.dumps(out["no_lock_control_sequence"]), flush=True)
    v = out["row_lock"]["verify"]
    out["control_sensitive"] = (out["no_lock_control_max_plus_one"]["errors"] > 0 or not out["no_lock_control_max_plus_one"]["verify"]["chain_ok"]) and not out["no_lock_control_sequence"]["verify"]["chain_ok"]
    out["pass"] = bool(v["chain_ok"] and v["gapless"] and v["rows"] == (total // writers) * writers and v["lost_payloads"] == 0 and v["duplicated_payloads"] == 0 and out["control_sensitive"])
    out["disk_free_gb_after"] = disk_free_gb()
    save("C7_writers_v2", out)
    print("PASS" if out["pass"] else "FAIL")
