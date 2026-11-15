"""C8: concept job queue under skewed load.
Registered policy and bound (written to the WAL before the run):
  debounce D=1.0 s, max_wait M=5.0 s, lease 30 s, fold work 20 ms, 8 workers, poll 100 ms.
  PASS bound B = 7.0 s: every activity is covered by a fold that completes within B seconds of the activity
  (M + poll + fold + 1.9 s allowance for saturation), no two folds of one concept overlap in time,
  every hot concept is folded at least once per (M + 2 s) on average and its largest gap is <= B.
usage: c8_concept_queue.py [duration_s=90] [concepts=3000] [rate=400]"""
import bisect, multiprocessing as mp, random, sys, time
from common import *
from memsys import conceptq

D, M, LEASE, WORK, WORKERS, POLL, B = 1.0, 5.0, 30.0, 0.02, 8, 0.1, 7.0
T = "c8"

def activity(args):
    dur, n, rate, seed = args
    conn = db.connect(autocommit=True)
    rng = random.Random(seed)
    weights = [1.0 / (i + 1) ** 1.1 for i in range(n)]       # zipf-like skew: a few very hot concepts
    cum, s = [], 0.0
    for w in weights:
        s += w; cum.append(s)
    log = []
    t_end = time.time() + dur
    nxt = time.time()
    while time.time() < t_end:
        c = bisect.bisect(cum, rng.random() * s)
        with conn.transaction():
            conceptq.touch_concepts(conn.cursor(), T, [f"k{c:05d}"], D, M)
        log.append((c, time.time()))
        nxt += 1.0 / rate
        d = nxt - time.time()
        if d > 0:
            time.sleep(d)
    return log

def worker(args):
    stop_at, = args
    conn = db.connect(autocommit=True)
    folds, rejected = [], 0
    idle_after = None
    while True:
        rows = conceptq.claim(conn, limit=4, lease=LEASE)
        if not rows:
            if time.time() > stop_at and conn.execute("select count(*) from concepts where dirty").fetchone()[0] == 0:
                break
            time.sleep(POLL)
            continue
        for tenant, cid, token, gen in rows:
            t_claim = time.time()
            time.sleep(WORK)                                      # stand-in for the fold's work
            r = conceptq.commit_fold(conn, tenant, cid, token, gen, ([], [], "d"), D, M)
            folds.append((cid, t_claim, time.time(), gen, r))
            rejected += r == "rejected"
    return folds, rejected

def lease_expiry_case():
    """Worker A stalls past its lease; B re-claims and commits; A's late commit must be rejected."""
    conn = db.connect(autocommit=True)
    conn.execute("delete from concepts where tenant_id='c8x'")
    with conn.transaction():
        conceptq.touch_concepts(conn.cursor(), "c8x", ["stall"], 0.0, 0.0)
    time.sleep(0.05)
    a = conceptq.claim(conn, limit=1, lease=1.0)
    time.sleep(1.3)
    b = conceptq.claim(conn, limit=1, lease=30.0)
    rb = conceptq.commit_fold(conn, b[0][0], b[0][1], b[0][2], b[0][3], ([], [], "B"))
    ra = conceptq.commit_fold(conn, a[0][0], a[0][1], a[0][2], a[0][3], ([], [], "A-stale"))
    row = conn.execute("select digest, fold_count from concepts where tenant_id='c8x'").fetchone()
    return {"a_claimed": bool(a), "b_claimed_after_expiry": bool(b), "b_commit": rb, "a_late_commit": ra, "final_digest": row[0], "fold_count": row[1],
            "pass": ra == "rejected" and rb == "committed" and row == ("B", 1)}

def main():
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    rate = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    conn = db.connect(autocommit=True)
    db.reset_schema(conn)
    ctx = mp.get_context("fork")
    t0 = time.time()
    with ctx.Pool(WORKERS + 1) as p:
        aw = p.map_async(activity, [(dur, n, rate, 16096)])
        ww = p.map_async(worker, [(t0 + dur,)] * WORKERS)
        alog = aw.get()[0]
        wres = ww.get()
    folds = [f for r in wres for f in r[0]]
    byc = {}
    for cid, tc, td, gen, r in folds:
        byc.setdefault(cid, []).append((tc, td, r))
    for v in byc.values():
        v.sort()
    # latency of each activity = completion time of the first COMMITTED-or-dirty fold claimed after it
    lat, lat_hot, lat_cold = [], [], []
    hot_ids = set(range(5))
    unserved = 0
    for c, ta in alog:
        cid = f"k{c:05d}"
        lst = byc.get(cid, [])
        i = bisect.bisect_left([x[0] for x in lst], ta)
        if i >= len(lst):
            unserved += 1; continue
        l = lst[i][1] - ta
        lat.append(l); (lat_hot if c in hot_ids else lat_cold).append(l)
    overlaps = sum(1 for v in byc.values() for a, b in zip(v, v[1:]) if b[0] < a[1])
    hot_stats = {}
    for c in sorted(hot_ids):
        v = byc.get(f"k{c:05d}", [])
        gaps = [b[0] - a[0] for a, b in zip(v, v[1:])]
        hot_stats[f"k{c:05d}"] = {"activities": sum(1 for x in alog if x[0] == c), "folds": len(v), "max_gap_between_fold_claims_s": round(max(gaps), 2) if gaps else None,
                                   "mean_gap_s": round(sum(gaps) / len(gaps), 2) if gaps else None}
    still_dirty = conn.execute("select count(*) from concepts where dirty").fetchone()[0]
    out = {"workload": f"{n} concepts, zipf s=1.1 skew, {rate} activities/s for {dur}s ({len(alog)} activities), {WORKERS} worker processes, fold work {WORK*1000:.0f} ms, PG16 4cpu on t2-dev",
           "registered": {"debounce_s": D, "max_wait_s": M, "lease_s": LEASE, "bound_B_s": B},
           "activities": len(alog), "folds": len(folds), "folds_rejected": sum(r[1] for r in wres), "unserved_activities": unserved, "still_dirty_after_drain": still_dirty,
           "latency_all_s": percentiles(lat), "latency_hot_s": percentiles(lat_hot), "latency_cold_s": percentiles(lat_cold),
           "concepts_folded": len(byc), "overlapping_folds_same_concept": overlaps, "hot_concepts": hot_stats,
           "lease_expiry_case": lease_expiry_case()}
    mx = max(lat) if lat else None
    out["pass"] = bool(mx is not None and mx <= B and overlaps == 0 and unserved == 0 and still_dirty == 0 and out["lease_expiry_case"]["pass"]
                       and all(h["folds"] >= max(1, dur / (M + 2) * 0.8) for h in hot_stats.values()))
    save("C8_concept_queue", out)
    print(json.dumps(out, indent=1))

if __name__ == "__main__":
    main()
