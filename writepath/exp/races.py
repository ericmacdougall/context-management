"""Races with REAL processes and RANDOM timing (review: no deterministic hook repeats). Each run starts two processes
with independent random delays (seeded RNG; seeds recorded), records each side's critical instants on the database
clock (clock_timestamp()), and decides afterwards whether the run actually OVERLAPPED. Judged per THRESHOLDS
amendment 3: N runs per arm, verdict computed only over overlapping runs, Wilson 95% bounds reported.

R1 stale projector vs erasure placeholders. P1 reads X's events (before the mark) and bulk-writes them after a random
   delay; P2 erases X after a random delay. Overlap = P1 read before the mark AND P1 bulk after the placeholders.
   Property: after both finish, X documents in ES = 0. Control (one patch, in P2): placeholders are DELETEs instead of
   content-free placeholder documents at version 2^62 (a late create then resurrects the document).
R2 writers vs the erasure mark. P1 appends X events in a loop with random gaps; P2 erases X after a random delay.
   Overlap = P1 made attempts both before and after the mark committed. Property: after both finish, the store's own
   residue function reports 0 for X (no event, fact, inbox or quarantine content of X outside the erasure).
   Control (one SQL patch): events_guard without the erased-subject check (WPS03).
R3 zombie session vs takeover. P1 (holder, epoch e) keeps appending with random gaps; P2 takes the session over
   (force) after a random delay and appends. Overlap = P1's attempts span the takeover. Property: no event at epoch e
   is ingested after the takeover was granted (an in-flight transaction blocks the takeover: F6 measures that).
   Control (one SQL patch): wp_fence with the epoch comparison removed.
The race experiments skip the ES expunge/verify (a stub verifier): the property is checked by the harness itself.
usage: python -m exp.races [R1 R2 R3] [N]"""
import json
import math
import multiprocessing as mp
import random
import sys
import time

from exp.common import write_result
from exp.patches import patched, variant, NOOP
from exp.util import tenant, msg, session, es_count_all, env

SEED0 = 16096


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - h) / d, 4), round((c + h) / d, 4)]


def _now(conn):
    return conn.execute("select extract(epoch from clock_timestamp())::float8").fetchone()[0]


class _StubVerifier:
    def complete(self, jid, expunge=True, wait_s=None):
        return {"done": False, "receipt_state": "skipped", "skipped": "race experiment: verification by the harness"}


# ---------------------------------------------------------------- R1
def _r1_projector(t, delay, q):
    from wp import projector
    conn, ks, es = env()
    marks = {}

    def hk(name, data=None):
        if name == "mirror_after_read" and "read" not in marks:
            marks["read"] = _now(conn)
            time.sleep(delay)
            marks["bulk"] = _now(conn)
    p = projector.Projector(conn, ks, es, t, hook=hk)
    p.run(max_rounds=20); p.stop()
    q.put(("proj", marks))


def _r1_eraser(t, delay, arm, q):
    from wp import erasure, families as F
    conn, ks, es = env()
    time.sleep(delay)
    if arm == "control":                                     # PATCH: delete instead of a placeholder document
        def as_delete(es_, t_, f, ids, batch=1000):
            F.ensure_family(es_, t_, f)
            if ids:
                es_.bulk_fenced([({"delete": {"_index": F.erase_alias(t_, f), "_id": d}}, None) for d in ids])
            return len(ids), 0
        F.place_placeholders = as_delete
    marks = {}

    def hk(name, data=None):
        marks[name] = _now(conn)
    rec = erasure.erase(conn, ks, es, t, "X", hook=hk, verifier=_StubVerifier(), physical="wait")
    q.put(("erase", dict(marks, error=rec["error_type"])))


def r1(arm, seed):
    rng = random.Random(seed)
    conn, ks, es = env()
    t = tenant("r1")
    from wp import log
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"X secret {i}", "X") for i in range(5)] + [msg(9, "Y stays", "Y")])
    q = mp.Queue()
    a = mp.Process(target=_r1_projector, args=(t, rng.uniform(0.2, 2.5), q))
    b = mp.Process(target=_r1_eraser, args=(t, rng.uniform(0.0, 1.5), arm, q))
    a.start(); b.start(); a.join(60); b.join(120)
    got = dict(q.get(timeout=5) for _ in range(2))
    p, e = got["proj"], got["erase"]
    time.sleep(0.5)
    x = es_count_all(es, t, {"term": {"subject_id": "X"}})
    overlap = bool(p.get("read") and e.get("after_mark") and p["read"] < e["after_mark"] and p.get("bulk", 0) > e.get("after_placeholders", 1e18))
    return {"holds": x == 0, "overlap": overlap, "x_docs_after": x, "instants": {"proj": p, "erase": e}}


# ---------------------------------------------------------------- R2
def _r2_writer(t, sid_epoch, gaps, q):
    from wp import log, lease as L
    conn, ks, es = env()
    lease = L.Lease(t, L.session_scope("w"), sid_epoch, "w", None)
    att = []
    for g in gaps:
        time.sleep(g)
        t0 = _now(conn)
        try:
            r = log.append(conn, ks, lease, [msg(int(t0 * 1000) % 100000, "X late secret", "X")])
            out = "accepted" if r["accepted"] else ("quarantined" if r.get("quarantined") else "dropped")
        except Exception as ex:  # noqa: BLE001
            out = type(ex).__name__
        att.append((t0, out))
    q.put(("writer", att))


def _r2_eraser(t, delay, q):
    from wp import erasure
    conn, ks, es = env()
    time.sleep(delay)
    marks = {}

    def hk(name, data=None):
        marks[name] = _now(conn)
    rec = erasure.erase(conn, ks, es, t, "X", hook=hk, verifier=_StubVerifier(), physical="wait")
    q.put(("erase", dict(marks, error=rec["error_type"])))


def r2(arm, seed):
    rng = random.Random(seed)
    from wp import db, log, lease as L
    conn, ks, es = env()
    t = tenant("r2")
    w = L.acquire(conn, t, L.session_scope("w"), 120)
    log.append(conn, ks, w, [msg(0, "X first", "X")])
    q = mp.Queue()
    gaps = [rng.uniform(0.0, 0.15) for _ in range(25)]
    a = mp.Process(target=_r2_writer, args=(t, w.epoch, gaps, q))
    b = mp.Process(target=_r2_eraser, args=(t, rng.uniform(0.0, 1.5), q))
    a.start(); b.start(); a.join(120); b.join(120)
    got = dict(q.get(timeout=5) for _ in range(2))
    e, att = got["erase"], got["writer"]
    lo, hi = e.get("after_mark"), e.get("after_cascade")
    overlap = bool(lo and any(t0 < lo for t0, _ in att) and any(t0 >= lo for t0, _ in att))   # the writer was active across the mark
    res = db.admin().execute("select wp_erasure_pg_residue(%s, 'X')", (t,)).fetchone()[0]
    bad = {k: v for k, v in res.items() if v and k not in ("pg:mentions_unscanned",)}
    return {"holds": not bad, "overlap": overlap, "residue_nonzero": bad,
            "attempts": {o: sum(1 for _, x in att if x == o) for o in {x for _, x in att}}, "window": [lo, hi]}


# ---------------------------------------------------------------- R3
def _r3_zombie(t, epoch, gaps, q):
    from wp import log, lease as L
    conn, ks, es = env()
    lease = L.Lease(t, L.session_scope("s1"), epoch, "z", None)
    att = []
    for i, g in enumerate(gaps):
        time.sleep(g)
        t0 = _now(conn)
        try:
            r = log.append(conn, ks, lease, [msg(10 + i, "zombie write", "U")])
            out = "accepted" if r["accepted"] else "quarantined"
        except Exception as ex:  # noqa: BLE001
            out = type(ex).__name__
        att.append((t0, out))
    q.put(("zombie", att))


def _r3_taker(t, delay, q):
    from wp import log, lease as L
    conn, ks, es = env()
    time.sleep(delay)
    b, _ = L.acquire_wait(conn, t, L.session_scope("s1"), 30, force=True, deadline_s=30)
    m = {"takeover": _now(conn), "epoch": b.epoch}
    try:
        log.append(conn, ks, b, [msg(2, "new holder", "U")])
    except Exception as ex:  # noqa: BLE001
        m["append_error"] = type(ex).__name__
    q.put(("taker", m))


def r3(arm, seed):
    rng = random.Random(seed)
    from wp import db
    conn, ks, es = env()
    t = tenant("r3")
    a = session(conn, t, ttl=60)
    from wp import log
    log.append(conn, ks, a, [msg(0, "first", "U")])
    q = mp.Queue()
    pz = mp.Process(target=_r3_zombie, args=(t, a.epoch, [rng.uniform(0.0, 0.2) for _ in range(12)], q))
    pt = mp.Process(target=_r3_taker, args=(t, rng.uniform(0.2, 1.0), q))
    pz.start(); pt.start(); pz.join(60); pt.join(60)
    got = dict(q.get(timeout=5) for _ in range(2))
    z, k = got["zombie"], got["taker"]
    late = db.admin().execute("select count(*) from events where tenant_id=%s and lease_epoch=%s and extract(epoch from ts_ingested) > %s",
                              (t, a.epoch, k["takeover"])).fetchone()[0]
    overlap = bool(z and z[0][0] < k["takeover"] < z[-1][0])        # the zombie kept writing across the takeover
    return {"holds": late == 0, "overlap": overlap, "zombie_events_after_takeover": late, "taker": k,
            "zombie_outcomes": {o: sum(1 for _, x in z if x == o) for o in {x for _, x in z}}}


RACES = {
    "R1": (r1, lambda: NOOP, "place_placeholders issues DELETEs (in the eraser process)"),
    "R2": (r2, lambda: variant("events_guard", "if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;", ""),
           "events_guard without the erased-subject check"),
    "R3": (r3, lambda: variant("wp_fence", "if cur is null or cur <> e then", "if false then"), "wp_fence without the epoch comparison"),
}


def run(name, N):
    fn, ctl_patch, label = RACES[name]
    out = {"patch": label, "N": N}
    for arm in ("fenced", "control"):
        runs = []
        patch = ctl_patch() if arm == "control" else NOOP
        with patched(*patch):
            for i in range(N):
                seed = SEED0 * 1000 + (0 if arm == "fenced" else 500) + i
                try:
                    r = fn(arm, seed)
                except Exception as ex:  # noqa: BLE001
                    r = {"holds": None, "overlap": False, "error": f"{type(ex).__name__}: {str(ex)[:160]}"}
                r["seed"] = seed
                runs.append(r)
        ov = [r for r in runs if r["overlap"]]
        k_hold = sum(r["holds"] is True for r in ov)
        out[arm] = {"runs_total": N, "overlapped": len(ov), "errors": sum(r["holds"] is None for r in runs),
                    "holds_all": sum(r["holds"] is True for r in runs), "violated_all": sum(r["holds"] is False for r in runs),
                    "holds_overlapped": k_hold, "violated_overlapped": sum(r["holds"] is False for r in ov),
                    "wilson95_hold_rate_overlapped": wilson(k_hold, len(ov)),
                    "wilson95_violation_rate_overlapped": wilson(len(ov) - k_hold, len(ov)), "runs": runs}
    f, c = out["fenced"], out["control"]
    if f["overlapped"] < 20 or c["overlapped"] < 20:
        out["verdict"] = "INCONCLUSIVE"
    else:
        out["verdict"] = "PASS" if (f["violated_all"] == 0 and f["errors"] == 0 and c["violated_overlapped"] >= 1) else "FAIL"
    return out


if __name__ == "__main__":
    mp.set_start_method("spawn")
    which = [a for a in sys.argv[1:] if a in RACES] or list(RACES)
    N = int(next((a for a in sys.argv[1:] if a.isdigit()), "40"))
    for name in which:
        res = run(name, N)
        summ = {a: {k: v for k, v in res[a].items() if k != "runs"} for a in ("fenced", "control")}
        print(name, res["verdict"], json.dumps(summ, default=str)[:1200], flush=True)
        write_result(f"races_{name}", res)
