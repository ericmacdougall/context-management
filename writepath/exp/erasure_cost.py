"""Erasure cost (measurement, no PASS): completion time against subject size and tenant size, under real concurrent
projector load, plus concurrent erasures of different subjects and of the same subject.

For each tenant size T (docs in Postgres and ES, filler spread over 1,000 other subjects) the tenant holds subjects
S10, S1000 (and S100000 when T allows) and two extra subjects A1000, B1000. A LOAD process appends filler events at a
fixed rate and runs the projector continuously the whole time; it records, per second, how many events it appended
and how many reached ES, so the projection delay during each erasure is measured, not modelled. Then:
  1. each S-subject is erased alone (receipt step timings: mark, rescan, cascade, placeholders, key, verify);
  2. A1000 and B1000 are erased concurrently in two processes (both must be done; wall-clock overlap recorded);
  3. a second request for a subject whose erasure is running returns the same job and its runner gets
     'claimed_elsewhere' (same subject serialises).
The per-document design takes no projector lease and no tenant-wide lock: the measured projection delay is the
answer to "projection pause". Two more processes run the whole time:
  OTHER   a second tenant appends one event and projects it every 0.5 s and records append latency and time-to-visible
          in ES (what an unrelated tenant experiences during the erasures, including the key-table VACUUM FULL);
  SAMPLER every 0.2 s, cluster-wide: backends waiting on a lock, and the oldest open non-idle transaction (an upper
          bound on any lock hold), so lock waits and hold times are observed rather than inferred.
The key purge wait is the receipt's steps.key_purge.waited_s. Stops before building a size if free disk < 20 GB.
A 100,000-event subject is placed only when the tenant holds >= 200,000 documents. usage: python -m exp.erasure_cost <tenant sizes, e.g. 10000 100000 1000000>"""
import json
import multiprocessing as mp
import sys
import time

from exp.common import write_result
from exp.util import tenant, msg, session, env, es_count_all

LOAD_RATE = 50          # filler events per second during the measurements


def _build(t, total, subjects):
    from wp import log, projector
    conn, ks, es = env()
    s = session(conn, t, ttl=600)
    t0 = time.time()
    plan = [(sub, n) for sub, n in subjects.items()]
    filler = total - sum(subjects.values())
    i = 0
    for sub, n in plan + [("F", filler)]:
        done = 0
        while done < n:
            k = min(1000, n - done)
            evs = [msg(i + j, f"{sub} event {i + j}", sub if sub != "F" else f"F{(i + j) % 1000}") for j in range(k)]
            log.append(conn, ks, s, evs)
            done += k
            i += k
    t_app = time.time() - t0
    p = projector.Projector(conn, ks, es, t, ttl_s=600, batch=2000)
    t0 = time.time()
    p.run(max_rounds=100000); p.stop()
    return {"append_s": round(t_app, 1), "project_s": round(time.time() - t0, 1), "events": i}


def _load(t, stop, q):
    """Append LOAD_RATE events/s and project continuously; report per-second appended/projected counts."""
    from wp import log, projector, lease as L
    conn, ks, es = env()
    s = L.acquire(conn, t, L.session_scope("load"), 600, force=True)
    p = projector.Projector(conn, ks, es, t, ttl_s=60, batch=2000)
    series, n, k = [], 0, 0
    while not stop.is_set():
        t0 = time.time()
        log.append(conn, ks, s, [msg(10_000_000 + n + j, f"load {n + j}", f"L{(n + j) % 50}") for j in range(LOAD_RATE)])
        n += LOAD_RATE
        while p.step() > 0:
            pass
        k = es_count_all(es, t, {"prefix": {"subject_id": "L"}})
        series.append((round(t0, 2), n, k))
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    p.stop()
    q.put(series)


def _other(stop, q):
    """An unrelated tenant: one append + projection every 0.5 s; (t, append_s, visible_s) per tick."""
    from wp import projector
    conn, ks, es = env()
    t = tenant("ecother")
    s = session(conn, t, "o1", ttl=600)
    p = projector.Projector(conn, ks, es, t, ttl_s=600, batch=200)
    out, n = [], 0
    while not stop.is_set():
        t0 = time.time()
        from wp import log
        log.append(conn, ks, s, [msg(n, f"other {n}", "O1")])
        t1 = time.time()
        while es_count_all(es, t, {"match_all": {}}) < n + 1 and time.time() - t1 < 60:
            p.step()
        out.append((round(t0, 2), round(t1 - t0, 3), round(time.time() - t1, 3)))
        n += 1
        time.sleep(max(0.0, 0.5 - (time.time() - t0)))
    p.stop()
    q.put(out)


def _sampler(stop, q):
    from wp import db
    c = db.admin()
    out = []
    while not stop.is_set():
        r = c.execute("select count(*) filter (where wait_event_type = 'Lock'), coalesce(max(extract(epoch from clock_timestamp() - xact_start))"
                      " filter (where state <> 'idle' and backend_type = 'client backend' and pid <> pg_backend_pid()), 0)"
                      " from pg_stat_activity").fetchone()
        out.append((round(time.time(), 2), int(r[0]), round(float(r[1]), 3)))
        time.sleep(0.2)
    q.put(out)


def _window(series, a, b, cols):
    pts = [x for x in series if a <= x[0] <= b]
    return {"samples": len(pts), **{f"max_{name}": (max(x[i] for x in pts) if pts else None) for i, name in cols}}


def _erase(t, sub, q):
    from wp import erasure
    conn, ks, es = env()
    q.put((sub, erasure.erase(conn, ks, es, t, sub)))


def measure(total):
    from wp import db, erasure
    t = tenant("ec")
    subjects = {"S10": 10, "S1000": 1000, "A1000": 1000, "B1000": 1000}
    if total >= 200_000:
        subjects["S100000"] = 100_000
    out = {"tenant_docs": total, "subjects": subjects}
    out["build"] = _build(t, total, subjects)
    stop, q, qo, qs = mp.Event(), mp.Queue(), mp.Queue(), mp.Queue()
    lp = mp.Process(target=_load, args=(t, stop, q)); lp.start()
    op = mp.Process(target=_other, args=(stop, qo)); op.start()
    sp = mp.Process(target=_sampler, args=(stop, qs)); sp.start()
    time.sleep(5)
    conn, ks, es = env()
    out["single"] = {}
    for sub in [x for x in subjects if x.startswith("S")]:
        t0 = time.time()
        rec = erasure.erase(conn, ks, es, t, sub)
        out["single"][sub] = {"events": subjects[sub], "wall_s": round(time.time() - t0, 2), "done": rec["done"], "t0": round(t0, 2),
                              "t1": round(time.time(), 2), "steps": rec["steps"], "error_type": rec["error_type"]}
    # two different subjects concurrently
    qa = mp.Queue()
    pa = [mp.Process(target=_erase, args=(t, sub, qa)) for sub in ("A1000", "B1000")]
    t0 = time.time()
    [x.start() for x in pa]
    got = dict(qa.get(timeout=900) for _ in pa)
    [x.join() for x in pa]
    out["concurrent_window"] = (round(t0, 2), round(time.time(), 2))
    out["concurrent_different"] = {"wall_s": round(time.time() - t0, 2), "both_done": all(r["done"] for r in got.values()),
                                   "per_subject": {k: {"seconds": r["seconds"], "done": r["done"], "steps": r["steps"]} for k, r in got.items()}}
    # same subject: a second request while the first runs
    jid = erasure.request(conn, t, "F7")
    first = mp.Process(target=_erase, args=(t, "F7", qa)); first.start()
    time.sleep(0.3)
    jid2 = erasure.request(db.connect(), t, "F7")
    second = erasure.run(db.connect(), ks, es, jid2)
    first.join(900)
    r1 = qa.get(timeout=60)[1]
    out["same_subject"] = {"same_job": jid == jid2, "second_runner": second["error_type"] or ("done" if second["done"] else "?"),
                           "first_done": r1["done"]}
    stop.set()
    series = q.get(timeout=120)
    other, samp = qo.get(timeout=120), qs.get(timeout=120)
    lp.join(60); op.join(60); sp.join(60)
    # projection delay during each erasure window = appended - projected at the sampled seconds inside the window
    def lag(a, b):
        pts = [(n - k) for ts, n, k in series if a <= ts <= b]
        return {"samples": len(pts), "max_backlog_events": max(pts) if pts else None,
                "max_backlog_s": round(max(pts) / LOAD_RATE, 2) if pts else None, "rate_events_per_s": LOAD_RATE}
    out["projection_during"] = {sub: lag(v["t0"], v["t1"]) for sub, v in out["single"].items()}
    out["projection_baseline"] = lag(series[0][0], series[0][0] + 5) if series else None
    out["load_seconds"] = len(series)
    wins = {sub: (v["t0"], v["t1"]) for sub, v in out["single"].items()}
    wins["A1000+B1000"] = tuple(out["concurrent_window"])
    out["other_tenant_during"] = {k: _window(other, a, b, [(1, "append_s"), (2, "visible_s")]) for k, (a, b) in wins.items()}
    out["other_tenant_baseline"] = _window(other, other[0][0], other[0][0] + 5, [(1, "append_s"), (2, "visible_s")]) if other else None
    out["locks_during"] = {k: _window(samp, a, b, [(1, "lock_waiters"), (2, "oldest_open_xact_s")]) for k, (a, b) in wins.items()}
    out["key_purge"] = {sub: (v["steps"] or {}).get("key_purge") for sub, v in out["single"].items()}
    return out


if __name__ == "__main__":
    mp.set_start_method("spawn")
    sizes = [int(a) for a in sys.argv[1:] if a.isdigit()] or [10000]
    import os
    for n in sizes:
        st = os.statvfs(os.path.expanduser("~"))
        free_gb = st.f_bavail * st.f_frsize / 1e9
        if free_gb < 20:
            print("erasure_cost STOP free disk", round(free_gb, 1), "GB < 20 before size", n, flush=True)
            break
        res = measure(n)
        res["free_disk_gb_before"] = round(free_gb, 1)
        print("erasure_cost", n, json.dumps({k: res[k] for k in ("build", "single", "concurrent_different", "same_subject", "projection_during",
                                                                 "other_tenant_during", "other_tenant_baseline", "locks_during", "key_purge")}, default=str)[:4000], flush=True)
        write_result(f"erasure_cost_{n}", res)
