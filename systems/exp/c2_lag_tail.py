"""C2: projection lag and tail scan. usage: c2_lag_tail.py [steady_s=120] [rate=50] [pause_s=60]"""
import datetime as dt, subprocess, sys, threading, time, uuid
from common import *
from memsys import store, reader, fusion

T = "c2"
PY = sys.executable

def start_projector():
    return subprocess.Popen([PY, "-u", "exp/projector_main.py", T, "1", "1", "500"], stdout=subprocess.DEVNULL, stderr=open(f"{RES}/c2_projector.err", "ab"))

def mkev(text):
    return dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=text)

def token():
    return "zq" + uuid.uuid4().hex[:10]

def main():
    steady_s = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rate = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    pause_s = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    conn, lock, es = conns()
    db.reset_schema(conn)
    esx.install_templates(es); esx.drop_tenant(es, T)
    out = {"workload": f"{rate} single-event appends/s for {steady_s} s into one tenant (5 sessions), projector batch 500 with explicit refresh per batch; PG16 + ES 9.5.6 single node on t2-dev (shared)"}
    store.append_events(conn, T, "warm", [mkev("warm up event")])
    proj = start_projector()
    time.sleep(3)

    # ---- A. steady-state append -> searchable latency (probe every 10th event)
    commits, found = {}, {}
    stop = threading.Event()
    probes = []
    def prober():
        pes = ES()
        while not stop.is_set() or probes:
            if not probes:
                time.sleep(0.01); continue
            eid, t0 = probes.pop(0)
            while time.time() - t0 < 30:
                r = pes.req("POST", f"/sys-{T}-events/_search", {"size": 0, "query": {"ids": {"values": [eid]}}, "track_total_hits": True})[1]
                if r["hits"]["total"]["value"] > 0:
                    found[eid] = time.time() - t0; break
                time.sleep(0.02)
            else:
                found[eid] = None
    th = threading.Thread(target=prober); th.start()
    wconn = db.connect(autocommit=True)
    t_end = time.time() + steady_s; nxt = time.time(); n = 0
    while time.time() < t_end:
        ids, _ = store.append_events(wconn, T, f"s{n % 5}", [mkev(f"steady note {n}")])
        tc = time.time(); commits[ids[0]] = tc
        if n % 10 == 0:
            probes.append((ids[0], tc))
        n += 1; nxt += 1.0 / rate
        d = nxt - time.time()
        if d > 0: time.sleep(d)
    stop.set(); th.join()
    lat = [v for v in found.values() if v is not None]
    out["steady"] = {"appends": n, "probed": len(found), "not_found_in_30s": sum(v is None for v in found.values()),
                     "append_to_searchable_s": percentiles(lat), "mean_s": round(sum(lat) / len(lat), 3)}
    print("steady", out["steady"], flush=True)

    # ---- B. read-after-write via tail scan, 100 trials (projector running)
    rd = reader.Reader(conn, es)
    via, es_only_hits = {"es": 0, "tail": 0}, 0
    ok = 0
    for i in range(100):
        tok = token()
        ids, _ = store.append_events(wconn, T, "trial", [mkev(f"trial {tok} marker")])
        r = rd.search(T, tok, k=10)                           # issued immediately after the commit
        hit = next((h for h in r["hits"] if h["id"] == ids[0]), None)
        ok += hit is not None
        if hit: via[hit["via"]] += 1
        r2 = rd.search(T, tok, k=10, tail=False)              # same read without the tail scan
        es_only_hits += any(h["id"] == ids[0] for h in r2["hits"])
        time.sleep(0.05)
    out["read_after_write"] = {"trials": 100, "found_with_tail": ok, "found_via": via, "found_without_tail_scan": es_only_hits}
    print("raw", out["read_after_write"], flush=True)

    # ---- C. projector paused for pause_s: every event still readable
    proj.kill(); proj.wait()
    hwm0 = rd.hwm(T)
    paused_ids, toks = [], []
    t_end = time.time() + pause_s; nxt = time.time(); k = 0
    while time.time() < t_end:
        tok = token()
        ids, _ = store.append_events(wconn, T, f"s{k % 5}", [mkev(f"paused {tok} note")])
        paused_ids.append(ids[0]); toks.append(tok); k += 1
        nxt += 0.1
        d = nxt - time.time()
        if d > 0: time.sleep(d)
    hwm1 = rd.hwm(T)
    found_c = sum(any(h["id"] == i for h in rd.search(T, tok, k=10)["hits"]) for i, tok in zip(paused_ids, toks))
    trunc = rd.search(T, toks[-1], k=10)["tail"]
    small = reader.Reader(conn, es, tail_bound=300)
    found_small, flagged = 0, 0
    for i, tok in zip(paused_ids, toks):
        r = small.search(T, tok, k=10)
        found_small += any(h["id"] == i for h in r["hits"]); flagged += r["tail"]["truncated"]
    out["paused"] = {"paused_s": pause_s, "events_written_while_paused": len(paused_ids), "hwm_unchanged": hwm0 == hwm1,
                     "found_with_default_tail_bound_2000": found_c, "tail_info_default": trunc,
                     "tail_bound_300": {"found": found_small, "queries_flagged_truncated": flagged, "of": len(paused_ids)}}
    print("paused", out["paused"], flush=True)
    # ---- D. catch-up after restart
    proj = start_projector(); t0 = time.time()
    while time.time() - t0 < 120:
        x = db.connect(autocommit=True).execute("select count(*) from events e where tenant_id=%s and (commit_xid, ingest_seq) > (select hwm_xid, hwm_seq from projection_state where view='events' and tenant_id=%s)", (T, T)).fetchone()[0]
        if x == 0: break
        time.sleep(0.2)
    out["catchup_s"] = round(time.time() - t0, 2)
    time.sleep(1.5)
    out["final_compare"] = compare_events(conn, es, [T])
    proj.kill(); proj.wait()
    s, p, c = out["steady"], out["paused"], out["read_after_write"]
    out["pass"] = (c["found_with_tail"] == 100 and p["found_with_default_tail_bound_2000"] == len(paused_ids) and s["not_found_in_30s"] == 0
                   and out["final_compare"]["missing"] == 0 and out["final_compare"]["duplicates"] == 0)
    save("C2_lag_tail", out)
    print(json.dumps(out, indent=1, default=str))

if __name__ == "__main__":
    main()
