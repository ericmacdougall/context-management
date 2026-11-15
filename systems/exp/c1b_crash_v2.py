"""C1b: idempotent mirror under crashes, rollover-like generation swaps, late/backfilled events, read-only and dropped buckets.
usage: c1b_crash.py [tenants=4] [sessions=100] [per_session=100] [crashes=50]
Crash methods: wrapper SIGKILL at a random time (child process started and killed by this script), plus the
projector SIGKILLing ITSELF at named points (derive_after_read, derive_after_insert, derive_before_commit,
bucket_create, mirror_after_read, mirror_mid_bulk, mirror_after_bulk, mirror_after_refresh, mirror_after_cursor)."""
import datetime as dt, os, random, subprocess, sys, time
os.environ.setdefault("MEMSYS_BUCKET_DAYS", "30")
from common import *
from memsys import gen, store, erasure, projector, kms
from memsys.projector import LOCK_SQL

PY = sys.executable
POINTS = ["time", "derive_after_read", "derive_after_insert", "derive_before_commit", "bucket_create", "mirror_after_read",
          "mirror_mid_bulk", "mirror_after_bulk", "mirror_after_refresh", "mirror_after_cursor"]

def classic_rollover_control(es):
    """The data-stream finding, reproduced with plain indices behind a write alias that rolls over: duplicates."""
    for i in ("sys-c1bctl-000001", "sys-c1bctl-000002"):
        es.req("DELETE", f"/{i}", ok=None)
    es.req("PUT", "/sys-c1bctl-000001", {"aliases": {"sys-c1bctl-w": {"is_write_index": True}}})
    doc = {"v": 1}
    es.req("PUT", "/sys-c1bctl-w/_create/EVT1", doc)
    es.req("PUT", "/sys-c1bctl-000002")
    es.req("POST", "/_aliases", {"actions": [{"add": {"index": "sys-c1bctl-000001", "alias": "sys-c1bctl-w", "is_write_index": False}},
                                           {"add": {"index": "sys-c1bctl-000002", "alias": "sys-c1bctl-w", "is_write_index": True}}]})
    s, j = es.req("PUT", "/sys-c1bctl-w/_create/EVT1", doc, ok=None)     # the retry after the rollover
    es.req("POST", "/sys-c1bctl-*/_refresh")
    n = es.req("POST", "/sys-c1bctl-*/_count", {"query": {"term": {"_id": "EVT1"}}})[1]["count"]
    for i in ("sys-c1bctl-000001", "sys-c1bctl-000002"):
        es.req("DELETE", f"/{i}", ok=None)
    return {"retry_status_after_rollover": s, "copies_of_one_id": n}

def run_projector(crash_point, tenants, kill_after):
    env = dict(os.environ)
    if crash_point != "time":
        env["MEMSYS_CRASH"] = f"{crash_point}:0.12"
    p = subprocess.Popen([PY, "-u", "exp/projector_main.py", ",".join(tenants), "1", "1", "300"], env=env, stdout=subprocess.DEVNULL,
                         stderr=open(f"{RES}/c1b_projector.err", "ab"))
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < kill_after:
        time.sleep(0.05)
    how = "self-killed" if p.poll() is not None else "wrapper-killed"
    if p.poll() is None:
        p.kill()
    p.wait()
    return how, p.returncode

def main():
    nt = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    ns = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    per = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    crashes = int(sys.argv[4]) if len(sys.argv) > 4 else 50
    tenants = [f"b{i:02d}" for i in range(nt)]
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es)
    for t in tenants:
        esx.drop_tenant(es, t)
    out = {"workload": f"{nt} tenants x {ns} sessions x {per} events = {nt*ns*per} events, bucket span {esx.BUCKET_DAYS} days (~18 buckets/tenant), projector batch 300, {crashes} abrupt stops; PG16+ES 9.5.6 on t2-dev",
           "classic_rollover_control": classic_rollover_control(es)}
    print(out["classic_rollover_control"], flush=True)
    rng = random.Random(7)
    t0 = time.time()
    for t in tenants:
        gen.load(conn, t, gen.gen_tenant(t, 7, ns, per), 7, store)
    out["load_s"] = round(time.time() - t0, 1)
    log = []
    for i in range(crashes):
        pt = POINTS[i % len(POINTS)]
        cs = lambda: conn.execute("select coalesce(sum(hwm_xid),0)::numeric from projection_state where hwm_seq < 4611686018427387904").fetchone()[0] + conn.execute("select count(*) from projection_state").fetchone()[0]
        c0 = cs()
        how, rc = run_projector(pt, tenants, rng.uniform(1.5, 7.0))
        progressed = cs() != c0
        log.append((i, pt, how + ("+progress" if progressed else "+no-progress"), rc))
        extra = None
        if i in (10, 30):      # rollover-like: swap a bucket's generation while a projector is running (fence must exclude it)
            t = rng.choice(tenants)
            b = esx.bucket_of(dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)) + rng.randint(0, 6)
            proj = subprocess.Popen([PY, "-u", "exp/projector_main.py", t, "1", "1", "200"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            lock.execute(f"select pg_advisory_lock({LOCK_SQL})", (t,))
            try:
                r = erasure.rebuild_events_bucket(conn, kms.get(), es, t, b)
            finally:
                lock.execute(f"select pg_advisory_unlock({LOCK_SQL})", (t,))
            time.sleep(0.5); proj.kill(); proj.wait()
            extra = f"generation swap {t} bucket {b}: {r}"
        if i in (15, 35):      # late/backfilled events (old timestamps) and a brand-new future bucket
            t = rng.choice(tenants)
            old = dt.datetime(2025, 1, 5, tzinfo=dt.timezone.utc); new = dt.datetime(2027, 2, 1, tzinfo=dt.timezone.utc)
            evs = [dict(ts_event=old + dt.timedelta(minutes=k), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=f"Zed's city is Oslo late{k}") for k in range(50)]
            evs += [dict(ts_event=new + dt.timedelta(minutes=k), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=f"Zed's city is Rome future{k}") for k in range(50)]
            store.append_events(conn, t, f"late{i}", evs)
            extra = f"{len(evs)} backfilled/future events into {t}"
        if i == 25:            # read-only bucket inside the retry horizon
            t = tenants[0]
            idx = [r["index"] for r in tenant_idx(es, t, "events")][0]
            import re as _re
            bk = int(_re.search(r"-events-b(\d+)-g", idx).group(1))
            bts = dt.datetime.fromtimestamp(bk * esx.BUCKET_DAYS * 86400 + 3600, dt.timezone.utc)
            conn.execute("select 1")
            store.append_events(conn, t, "ro-session", [dict(ts_event=bts + dt.timedelta(minutes=k), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=f"Zed's city is Rome ro{k}") for k in range(40)])
            es.req("PUT", f"/{idx}/_settings", {"index.blocks.write": True})
            how2, _ = run_projector("time", [t], 3.0)
            cur = conn.execute("select hwm_xid from projection_state where view='events' and tenant_id=%s", (t,)).fetchone()
            behind = conn.execute("select count(*) from events where tenant_id=%s and commit_xid > %s", (t, cur[0] if cur else 0)).fetchone()[0]
            es.req("PUT", f"/{idx}/_settings", {"index.blocks.write": None})
            extra = f"read-only bucket index {idx} for 3 s: tenant {t} mirror stalled, {behind} events behind the cursor (head-of-line stall), then unblocked"
        if extra:
            log[-1] = log[-1] + (extra,)
            print(extra, flush=True)
    out["crash_log"] = log
    fired = {}
    for x in log:
        fired.setdefault(x[1], {"self_killed": 0, "wrapper_killed": 0})[x[2].split("+")[0].replace("-", "_")] += 1
    out["kills_with_work_in_flight"] = {"self_killed_at_named_point": sum(1 for x in log if x[2].startswith("self-killed")),
                                        "wrapper_killed_after_progress": sum(1 for x in log if x[2].startswith("wrapper-killed+progress")),
                                        "wrapper_killed_without_progress": sum(1 for x in log if x[2].startswith("wrapper-killed+no-progress"))}
    out["crash_points_fired"] = fired
    print(fired)
    print("crashes done", sum(1 for x in log if x[2].startswith("self-killed")), "self-killed,", sum(1 for x in log if x[2].startswith("wrapper-killed")), "wrapper-killed", flush=True)
    # final: run to completion
    p = projector.Projector(conn, lock, es, recipes=[1], live_recipe=1, batch=300, tenants=tenants)
    t0 = time.time(); p.run(); out["final_catchup_s"] = round(time.time() - t0, 1)
    out["compare_events"] = compare_events(conn, es, tenants)
    out["compare_facts"] = compare_facts(conn, es, tenants, 1)
    print(out["compare_events"], out["compare_facts"], flush=True)
    # dropped bucket inside the horizon: gap appears (cursor is past it); reconcile detects; repair
    t = tenants[1]
    idx = [r["index"] for r in tenant_idx(es, t, "events")][3]
    es.req("DELETE", f"/{idx}")
    out["dropped_bucket"] = {"index": idx, "compare_after_drop": compare_events(conn, es, [t])}
    p.gen_ev.clear()
    p.run()
    out["dropped_bucket"]["compare_after_projector_rerun"] = compare_events(conn, es, [t])       # cursor is past it: still missing
    import re; b = int(re.search(r"-events-b(\d+)-g", idx).group(1))
    esx.ensure_events_bucket(es, conn, t, b)
    r = erasure.rebuild_events_bucket(conn, kms.get(), es, t, b)
    out["dropped_bucket"]["repair"] = str(r)
    out["dropped_bucket"]["compare_after_repair"] = compare_events(conn, es, [t])
    e = out["compare_events"]; f = out["compare_facts"]
    out["pass"] = (e["missing"] == 0 and e["duplicates"] == 0 and e["extra"] == 0 and e["hash_mismatch"] == 0 and e["wrong_bucket"] == 0
                   and f["missing"] == 0 and f["duplicates"] == 0 and f["extra"] == 0 and f["hash_mismatch"] == 0
                   and out["classic_rollover_control"]["copies_of_one_id"] == 2 and out["dropped_bucket"]["compare_after_repair"]["missing"] == 0)
    out["disk_free_gb"] = disk_free_gb()
    save("C1b_crash_v2", out)
    print("PASS" if out["pass"] else "FAIL")

def tenant_idx(es, t, kind):
    return sorted([r for r in esx.tenant_indices(es, t) if f"-{kind}-" in r["index"]], key=lambda r: r["index"])

if __name__ == "__main__":
    main()
