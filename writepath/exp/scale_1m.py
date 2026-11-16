"""Erasure at tenant scale (coordinator items 2 and 3 of the 4960f50 round; THRESHOLDS amendment 9).

At 7e8f28f the 100,000-event subject in a 1,000,000-document tenant ended in ClaimLost after 108.6 s, 1,000-event
cascades took ~13 s per batch at 1M, and verify took ~113 s. This experiment, per tenant size N (100,000 then
1,000,000 documents; filler over 1,000 other subjects):
  E  EXPLAIN: auto_explain (ANALYZE, BUFFERS, nested statements) of the first 3 fixed-size cascade batches of a
     separate subject X10000, read back from the server log (which statement costs what; buffers read/dirtied answer
     why the first batches after a bulk load are slower). Reported, no PASS.
  R  RESIDUE COST: wp_erasure_pg_residue for a 10-event subject, shipped function against the 4960f50 control body
     (exp/controls_4960f50.sql), 3 timings each.
  C  ERASURES through the real runner (claim, heartbeat, mark, rescan, cascade, placeholders, key, verifier):
     S10, S1000, S10000 and, at 1M only, S100000. Per subject: done, error, seconds, cascade seconds, batches, the
     longest batch, heartbeat transient failures, verify seconds, receipt state.
  H  HEARTBEAT (once, at the smallest N): while a job runs, another process holds its job row FOR UPDATE for 35 s
     (what one long cascade batch did at 4960f50). Fenced: the shipped heartbeat retries the lock timeout and the job
     is done. Control: the heartbeat without transient tolerance (any renewal error = lost, the 7e8f28f behaviour) ends
     in ClaimLost. R=3 per arm. Every real erasure in C must also show heartbeat_transient == 0 (the cascade no longer
     holds the job row).
Stops before a size if free disk < 20 GB. usage: python -m exp.scale_1m [sizes...]"""
import json
import multiprocessing as mp
import os
import pathlib
import subprocess
import sys
import time

from exp.common import write_result
from exp.erasure_cost import _build
from exp.util import tenant, env

PG_C = "emlab-pg-wp-16096"
HOLD_S = 35     # claim TTL 60 s, beat every 20 s, role lock_timeout 10 s: the beat at ~20 s fails at ~30 s, the next succeeds
CONTROLS = (pathlib.Path(__file__).resolve().parent / "controls_4960f50.sql").read_text()


def _ctl_fn(name):
    st = CONTROLS.index(f"create function {name}(")
    f = CONTROLS.index("$$", st)
    return CONTROLS[st:CONTROLS.index("$$;", f + 2) + 3]


def _swap(name, body_from):
    from exp.patches import function_sql
    new = function_sql(name).replace("create function", "create or replace function", 1)
    old = body_from(name).replace("create function", "create or replace function", 1)
    return ["set role wp_owner", old, "reset role"], ["set role wp_owner", new, "reset role"]


def explain_batches(t, subject, nbatches=3, batch=5000):
    from wp import db, erasure
    conn, _, _ = env()
    jid = erasure.request(conn, t, subject)
    adm = db.admin()
    adm.execute("select wp_erasure_mark(%s)", (jid,))
    adm.execute("select wp_erasure_rescan_done(%s, coalesce((select ident_xmax from erasure_jobs where job_id=%s), 0))", (jid, jid))
    adm.execute("load 'auto_explain'")
    for k, v in (("auto_explain.log_min_duration", "50ms"), ("auto_explain.log_analyze", "on"),
                 ("auto_explain.log_nested_statements", "on"), ("auto_explain.log_buffers", "on"), ("statement_timeout", "0")):
        adm.execute(f"set {k} = '{v}'")
    out = []
    for i in range(nbatches):
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
        t0 = time.time()
        n = adm.execute("select wp_erasure_cascade(%s, %s)", (jid, batch)).fetchone()[0]
        dt = round(time.time() - t0, 3)
        time.sleep(1.0)
        log = subprocess.run(["docker", "logs", "--since", since, PG_C], capture_output=True, text=True).stderr
        stmts = []
        for block in log.split("LOG:  duration: ")[1:]:
            lines = block.split("\n")
            try:
                dur = float(lines[0].split(" ms")[0])
            except ValueError:
                continue
            q = next((ln.strip()[len("Query Text: "):] for ln in lines if ln.strip().startswith("Query Text:")), "")
            plan = [ln.rstrip()[:200] for ln in lines[1:60] if ln.strip() and not ln.startswith("20")][:24]
            stmts.append({"ms": round(dur, 1), "query": " ".join(q.split())[:240], "plan": plan})
        stmts.sort(key=lambda d: -d["ms"])
        out.append({"batch": i, "rows": n, "wall_s": dt, "top": stmts[:5]})
        print("explain", i, n, dt, [(s["ms"], s["query"][:80]) for s in stmts[:5]], flush=True)
    adm.execute("reset statement_timeout")
    return out


def residue_cost(t, subject, reps=3):
    from wp import db
    from exp.patches import patched
    adm = db.admin()
    adm.execute("set statement_timeout = 0")

    def timed():
        r = []
        for _ in range(reps):
            t0 = time.time()
            adm.execute("select wp_erasure_pg_residue(%s, %s)", (t, subject)).fetchone()
            r.append(round(time.time() - t0, 3))
        return r
    res = {"shipped_s": timed()}
    ap, rv = _swap("wp_erasure_pg_residue", _ctl_fn)
    with patched(ap, rv):
        res["control_4960f50_s"] = timed()
    return res


def erase_one(t, subject):
    from wp import erasure
    from wp.verifier import Verifier
    conn, ks, es = env()
    jid = erasure.request(conn, t, subject)
    rec = erasure.run(conn, ks, es, jid, ttl_s=60, verifier=Verifier(es), cascade_deadline_s=1800, physical="wait")
    st = rec.get("steps", {})
    return {"done": rec["done"], "error_type": rec["error_type"], "seconds": rec.get("seconds"), "cascade_s": st.get("cascade_s"),
            "cascade_batches": st.get("cascade_batches"), "cascade_batch_max_s": st.get("cascade_batch_max_s"),
            "cascade_batch_s_first5": (st.get("cascade_batch_s") or [])[:5], "cascade_batch_s_last5": (st.get("cascade_batch_s") or [])[-5:],
            "cascade_conflicts": st.get("cascade_conflicts"), "heartbeat_transient": st.get("heartbeat_transient"),
            "mark_s": st.get("mark_s"), "rescan_s": st.get("rescan_s"), "placeholders_s": st.get("placeholders_s"),
            "verify_s": st.get("verify_s"), "verify_timings": st.get("verify_timings"), "expunge": st.get("expunge"), "key_purge": st.get("key_purge"),
            "residue": rec.get("residue"), "receipt_state": rec.get("receipt_state"), "logical_delay_s": rec.get("logical_delay_s"),
            "physical_s": st.get("physical_s")}


def _lock_job(jid, hold_s, q):
    from wp import db
    c = db.admin(autocommit=False)
    c.execute("set statement_timeout = 0")
    c.execute("select 1 from erasure_jobs where job_id=%s for update", (jid,))
    q.put("locked")
    time.sleep(hold_s)
    c.rollback()


def heartbeat_arm(t, subject, control):
    from wp import erasure
    from wp.verifier import Verifier
    conn, ks, es = env()
    jid = erasure.request(conn, t, subject)
    saved = erasure.HB_TOLERATE
    if control:
        erasure.HB_TOLERATE = False                         # 7e8f28f behaviour: any renewal error = claim lost
    try:
        def hook(name, _data=None):
            if name == "after_mark":
                q = mp.Queue()
                p = mp.Process(target=_lock_job, args=(jid, HOLD_S, q)); p.start()
                q.get(timeout=30)
                time.sleep(HOLD_S + 1)                       # the "long batch": the runner does nothing while the row is held
                p.join(10)
        rec = erasure.run(conn, ks, es, jid, ttl_s=60, hook=hook, verifier=Verifier(es), physical="wait")
    finally:
        erasure.HB_TOLERATE = saved
    return {"done": rec["done"], "error_type": rec["error_type"], "heartbeat_transient": rec.get("steps", {}).get("heartbeat_transient")}


def verdict(res):
    """THRESHOLDS amendment 9."""
    why = []
    for n, r in res["sizes"].items():
        for s, e in r["erasures"].items():
            if not e["done"]:
                why.append(f"N={n} {s}: not done ({e['error_type']})")
            if e["heartbeat_transient"]:
                why.append(f"N={n} {s}: {e['heartbeat_transient']} heartbeat renewal failures (the batch must not block the renewal)")
            if (e["cascade_batch_max_s"] or 0) > 5:
                why.append(f"N={n} {s}: longest batch {e['cascade_batch_max_s']} s > 5 s")
        rc = r["residue"]
        if min(rc["shipped_s"]) > 1.0:
            why.append(f"N={n} residue {min(rc['shipped_s'])} s > 1 s")
    big = res["sizes"].get("1000000", {}).get("erasures", {})
    if "S100000" in big and "S10000" in big:
        a = big["S10000"]["cascade_s"] / 10000
        b = big["S100000"]["cascade_s"] / 100000
        res["per_event_cascade_ms_1M"] = {"S10000": round(a * 1000, 3), "S100000": round(b * 1000, 3)}
        if b > 2 * a:
            why.append(f"S100000 per-event cascade {b:.5f} s > 2 x S10000 {a:.5f} s (not linear)")
    else:
        why.append("1M S10000/S100000 missing")
    small, large = res["sizes"].get("100000", {}).get("erasures", {}), big
    if "S10000" in small and "S10000" in large:
        a, b = small["S10000"]["cascade_s"], large["S10000"]["cascade_s"]
        res["s10000_cascade_s_100k_vs_1M"] = [a, b]
        if b > 2 * a:
            why.append(f"S10000 cascade at 1M {b} s > 2 x at 100k {a} s (depends on tenant size)")
    rc = res["sizes"].get("1000000", {}).get("residue")
    if rc and min(rc["control_4960f50_s"]) < 10 * min(rc["shipped_s"]):
        why.append("residue control at 1M not >= 10x the shipped function (control does not discriminate)")
    hb = res.get("heartbeat", {})
    if [a["done"] for a in hb.get("fenced", [])] != [True] * 3:
        why.append(f"heartbeat fenced not 3/3 done: {hb.get('fenced')}")
    if [a["error_type"] for a in hb.get("control", [])] != ["ClaimLost"] * 3:
        why.append(f"heartbeat control not 3/3 ClaimLost: {hb.get('control')}")
    return {"pass": not why, "why": why}


def main(sizes):
    res = {"sizes": {}}
    for i, n in enumerate(sizes):
        st = os.statvfs(os.path.expanduser("~"))
        free = st.f_bavail * st.f_frsize / 1e9
        if free < 20:
            res["stopped"] = f"free disk {free:.1f} GB < 20 before N={n}"
            break
        t = tenant("sc")
        subj = {"S10": 10, "S1000": 1000, "S10000": 10000, "X10000": 10000, "H1": 200, "H2": 200, "H3": 200, "H4": 200, "H5": 200, "H6": 200}
        if n >= 1_000_000:
            subj["S100000"] = 100000
        r = {"tenant": t, "free_disk_gb_before": round(free, 1)}
        r["build"] = _build(t, n, subj)
        print("build", n, r["build"], flush=True)
        r["explain"] = explain_batches(t, "X10000")
        r["residue"] = residue_cost(t, "S10")
        print("residue", n, r["residue"], flush=True)
        r["erasures"] = {}
        for s in [k for k in ("S10", "S1000", "S10000", "S100000") if k in subj]:
            r["erasures"][s] = erase_one(t, s)
            print("erase", n, s, json.dumps(r["erasures"][s])[:600], flush=True)
        if i == 0:
            res["heartbeat"] = {"fenced": [heartbeat_arm(t, f"H{j}", False) for j in (1, 2, 3)],
                                "control": [heartbeat_arm(t, f"H{j}", True) for j in (4, 5, 6)]}
            print("heartbeat", res["heartbeat"], flush=True)
        res["sizes"][str(n)] = r
    res["verdict"] = verdict(res)
    print("scale_1m", json.dumps(res["verdict"]), flush=True)
    write_result("scale_1m", res)


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main([int(a) for a in sys.argv[1:]] or [100000, 1000000])
