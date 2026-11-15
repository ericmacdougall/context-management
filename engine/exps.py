"""F4 experiments (run on t2-dev under venv-new or venv-old: python -m engine.exps <name> [--small] [--tport N]).
Every PASS has a control that can fail. Every result file carries its provenance (git commit, times, servers, workers,
parameters read from the variables the run used); nothing in a result is typed by hand."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from . import ctl, lib, pgstore
from .common import DEMO_TENANT as TENANT
from .common import HOME

SMALL = "--small" in sys.argv
TPORT = int(sys.argv[sys.argv.index("--tport") + 1]) if "--tport" in sys.argv else 0   # 0 = pick a free port block
TPORT = TPORT or lib.pick_port()
RETAG = sys.argv[sys.argv.index("--retag") + 1] if "--retag" in sys.argv else ""
RID = hashlib.sha1(lib.RUN_ID.encode()).hexdigest()[:6]
PG_CONTAINER = "emlab-pg-engine-16096"


def sizes(full, small):
    return small if SMALL else full


def T(t):
    """A tag unique to this run. Session ids are `<tag>-<nnnn>` and are matched with an anchored pattern, so no tag is a
    prefix of another."""
    return f"{RETAG}{t}{RID}"


def suffix():
    return ("-small" if SMALL else "") + (f"-{RETAG}" if RETAG else "")


def arg(name, default, cast=float):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def count_results(tag, tenant=TENANT):
    return lib.q("select count(*) from events where tenant_id=%s and kind='turn_result' and session_id like %s escape '\\'",
                 tenant, ctl.like(tag))[0][0]


def crash_log(name):
    p = HOME / "logs" / f"{lib._u(name)}.crashes"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def exit_summary(rows):
    """What the abrupt exits actually hit (from the worker's own instrumentation, read after the run)."""
    by = {}
    for r in rows:
        k = r.get("where") or r.get("how") or "timed"
        by[k] = by.get(k, 0) + 1
    return {"exits": len(rows), "by_window": by,
            "with_activity_in_flight": sum(1 for r in rows if r.get("inflight")),
            "in_turn_effect": sum(1 for r in rows if r.get("inflight", {}).get("turn_effect")),
            "in_turn_append": sum(1 for r in rows if r.get("inflight", {}).get("turn_append"))}


# ------------------------------------------------------------------ F4a random worker exits + abrupt server stop
async def f4a():
    n, turns = sizes((100, 30), (6, 6))
    crashes = sizes(50, 4)
    bench_s, bench_t = sizes((60, 10), (6, 4))
    db = f"f4a{suffix()}-{int(time.time())}.db"
    lib.start_server("1.7.3", db, TPORT)
    client, _ = await lib.connect_retry()
    # 1) measure the drain rate of one worker (no crashes), then inject at half of it
    lib.start_worker("bench", "venv-old", "v1", "bench", "f4a-bench")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    bt = T("bn")
    t0 = time.time()
    tasks = await lib.launch(client, bt, bench_s, "f4a-bench", stats, turns=bench_t, period=0.05, expected=bench_t)
    await asyncio.gather(*tasks)
    while count_results(bt) < bench_s * bench_t and time.time() - t0 < 600:
        await asyncio.sleep(1)
    drain = bench_s * bench_t / (time.time() - t0)
    lib.stop_worker("bench")
    period = n / (0.5 * drain)
    duration = turns * period
    cmax = max(4, int(2 * (duration / crashes - 5)))
    lib.record(sessions=n, turns_each=turns, planned_exits=crashes, measured_drain_turns_per_s=round(drain, 2),
               inject_period_s=round(period, 2), inject_rate_turns_per_s=round(n / period, 2), planned_duration_s=round(duration),
               crash_after_max_s=cmax, effect_gap_s=0.05)
    # 2) the run: abrupt worker exits at random points, one abrupt server stop
    tag = T("a")
    name = "f4aloop"
    lib.start_loop(name, "venv-old", "v1", "hostA", "f4a", env={"ENGINE_EFFECT_GAP": "0.05", "CMAX": str(cmax)}, seed=21, crashes=crashes)
    t_run = time.time()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tasks = await lib.launch(client, tag, n, "f4a", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(0.4 * duration)
    t_srv = time.time()
    lib.stop_server(hard=True)
    srv_rc = lib.PROCS[f"server-{TPORT}"].returncode
    await asyncio.sleep(4)
    lib.start_server("1.7.3", db, TPORT)
    srv_down = round(time.time() - t_srv, 1)
    await asyncio.gather(*tasks)
    inject_done = round(time.time() - t_run, 1)
    st, waited = await lib.wait_done(client, tag, n, 3600)
    v = ctl.verify(tag, n, turns)
    rows = crash_log(name)
    lib.stop_worker(name)
    lib.stop_server()
    out = {"id": "F4a", "drain_bench": {"sessions": bench_s, "turns_each": bench_t, "turns_per_s": round(drain, 2)},
           "server_stop": {"abrupt": True, "returncode": srv_rc, "down_s": srv_down, "at_s": round(t_srv - t_run, 1)},
           "worker_exits": exit_summary(rows), "inject_wall_s": inject_done, "drain_wait_s": waited, "driver": stats,
           "statuses": st, "verify": v, "PASS": v["PASS"] and st == {"COMPLETED": n} and len(rows) >= crashes}
    lib.save(f"f4a{suffix()}", out)
    print(out["PASS"], out["worker_exits"], out["server_stop"])


# ------------------------------------------------------------------ F4b host move (+ zombie old worker)
async def f4b():
    n, turns, period, move_at = sizes((100, 20, 15, 60), (10, 6, 3, 10))
    zombie = "--zombie" in sys.argv
    lib.record(sessions=n, turns_each=turns, period_s=period, move_at_s=move_at, zombie_old_worker=zombie, gap_s=8)
    lib.start_server("1.7.3", f"f4b{suffix()}-{int(time.time())}.db", TPORT)
    knobs = {"ENGINE_EFFECT_GAP": "0.05", "ENGINE_CRASH_IN_GAP": "0.004"}    # A may end itself inside the effect window
    lib.start_worker("f4bA", "venv-old", "v1", "hostA", "f4b", env=knobs)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("p")
    tasks = await lib.launch(client, tag, n, "f4b", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(move_at)
    rcA = None if zombie else lib.stop_worker("f4bA", hard=True)
    hb = HOME / "hostB"     # another host: other working dir, other ENGINE_HOME with its own copy of the connection files
    if hb.exists():
        shutil.rmtree(hb)
    (hb / "code").mkdir(parents=True)
    shutil.copytree(HOME / "code/engine", hb / "code/engine")
    (hb / "pg").mkdir()
    for f in ("pg_port", "pgpass"):
        shutil.copy(HOME / "pg" / f, hb / "pg" / f)
    os.chmod(hb / "pg/pgpass", 0o600)
    controls = {}
    r0 = count_results(tag)
    if not zombie:
        # control 1: nobody polling the queue: the run must make no progress
        await asyncio.sleep(8)
        controls["no_worker_progress_in_8s"] = count_results(tag) - r0
        # control 2: a worker on the WRONG queue must not make progress either, and verify must say "incomplete"
        lib.start_worker("f4bW", "venv-old", "v1", "wrongq", "f4b-wrong", cwd=hb / "code", home=hb)
        r1 = count_results(tag)
        await asyncio.sleep(8)
        controls["wrong_queue_worker_progress_in_8s"] = count_results(tag) - r1
        lib.stop_worker("f4bW")
        early = ctl.verify(tag, n, turns)
        controls["verify_while_unfinished_PASS"] = early["PASS"]
        controls["verify_while_unfinished_problems"] = len(early["problems"])
    lib.start_worker("f4bB", "venv-old", "v1", "hostB", "f4b", cwd=hb / "code", home=hb, env={"ENGINE_EFFECT_GAP": "0.05"})
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 1200)
    final = ctl.verify(tag, n, turns)
    calls = lib.q("select worker, count(*) from ext_calls where tenant_id=%s and key like %s escape '\\' group by 1",
                  TENANT, f"{TENANT}/eff/{TENANT}/" + ctl.like(tag))
    rowsA = crash_log("f4bA")
    lib.stop_worker("f4bB")
    if zombie:
        lib.stop_worker("f4bA")
    lib.stop_server()
    out = {"id": "F4b" + ("-zombie" if zombie else ""), "worker_A_exit_code": rcA, "worker_A_self_exits": exit_summary(rowsA),
           "workflow_statuses": st, "wait_s": waited, "controls": controls, "verify": final,
           "external_calls_by_worker": {r[0]: r[1] for r in calls}, "driver": stats}
    ok = final["PASS"] and st == {"COMPLETED": n} and any(r[0] == "hostB" for r in calls)
    if zombie:
        ok = ok and {r[0] for r in calls} >= {"hostA", "hostB"}
    else:
        ok = ok and controls["no_worker_progress_in_8s"] == 0 and controls["wrong_queue_worker_progress_in_8s"] == 0 \
            and not controls["verify_while_unfinished_PASS"]
    out["PASS"] = ok
    lib.save(f"f4b{'z' if zombie else ''}{suffix()}", out)
    print(out["PASS"], out["external_calls_by_worker"], controls)


# ------------------------------------------------------------------ F4c long waits
async def f4c():
    nA, nB, appr_to, roll_s = sizes((30, 30, 240, 180), (4, 4, 40, 30))
    DBC = f"f4c{suffix()}-{int(time.time())}.db"
    lib.record(approval_sessions=nA, rollup_sessions=nB, approval_timeout_s=appr_to, rollup_s=roll_s)
    lib.start_server("1.7.3", DBC, TPORT)
    lib.start_worker("f4cA", "venv-new", "v1", "hostA", "f4c")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t0 = time.time()
    tagA, tagB = T("r"), T("s")
    tA = await lib.launch(client, tagA, nA, "f4c", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr_to)
    tB = await lib.launch(client, tagB, nB, "f4c", stats, turns=0, rollup_s=roll_s, stop_after_rollups=1)
    await asyncio.gather(*tA)
    log, early = [], {}
    sc = appr_to / 240.0

    async def at(t, what, fn):
        d = t - (time.time() - t0)
        if d > 0:
            await asyncio.sleep(d)
        rc = await fn()
        log.append({"t": round(time.time() - t0, 1), "what": what, "exit_code": rc})

    async def early_check():
        early["approval_resolved"] = sum(len(lib.events_of(s, "approval_timeout")) + len(lib.events_of(s, "approval_granted"))
                                         for s in ctl.sids(tagA, nA))
        early["rollups"] = sum(len(lib.events_of(s, "rollup")) for s in ctl.sids(tagB, nB))

    async def crash():
        rc = lib.stop_worker("f4cA", hard=True)
        await asyncio.sleep(3)
        lib.start_worker("f4cA", "venv-new", "v1", "hostA", "f4c")
        return rc

    async def server_hard():
        lib.stop_server(hard=True)
        rc = lib.PROCS[f"server-{TPORT}"].returncode
        await asyncio.sleep(5)
        lib.start_server("1.7.3", DBC, TPORT)
        return rc

    async def server_soft():
        lib.stop_server()
        rc = lib.PROCS[f"server-{TPORT}"].returncode
        await asyncio.sleep(3)
        lib.start_server("1.7.3", DBC, TPORT)
        return rc

    approved = ctl.sids(tagA, nA)[: nA // 2]

    async def approve():
        res = []
        for s in approved:
            res.append(await ctl.approve_session(client, s, TENANT))
        return {r: res.count(r) for r in set(res)}

    async def noop():
        return None

    await at(20 * sc + 5, "early_check", early_check)
    await at(30 * sc, "worker_exit_1", crash)
    await at(60 * sc, "server_abrupt_stop_restart", server_hard)
    await at(95 * sc, "worker_exit_2", crash)
    await at(120 * sc, "approve_half", approve)
    await at(150 * sc, "server_graceful_restart", server_soft)
    await at(170 * sc, "worker_exit_3", crash)
    stA, _ = await lib.wait_done(client, tagA, nA, appr_to * 3)
    stB, _ = await lib.wait_done(client, tagB, nB, roll_s * 3)
    rowsA, rowsB = [], []
    for s in ctl.sids(tagA, nA):
        req, fin = lib.events_of(s, "approval_requested"), lib.events_of(s, "session_finished")
        res = lib.events_of(s, "approval_granted") + lib.events_of(s, "approval_timeout")
        dl = json.loads(req[0][3])["deadline_ms"] / 1000 if req else None
        rowsA.append({"sid": s, "requests": len(req), "resolutions": len(res), "kind": res[0][2] if res else None,
                      "finished": len(fin), "late_s": round(res[0][4].timestamp() - dl, 2) if res and dl else None})
    for s in ctl.sids(tagB, nB):
        ro, st_ev, fin = lib.events_of(s, "rollup"), lib.events_of(s, "session_started"), lib.events_of(s, "session_finished")
        rowsB.append({"sid": s, "rollups": len(ro), "finished": len(fin),
                      "late_s": round(ro[0][4].timestamp() - st_ev[0][4].timestamp() - roll_s, 2) if ro and st_ev else None})
    okA = all(r["requests"] == 1 and r["resolutions"] == 1 and r["finished"] == 1 for r in rowsA)
    okA = okA and all((r["kind"] == "approval_granted") == (r["sid"] in approved) for r in rowsA)
    okB = all(r["rollups"] == 1 and r["finished"] == 1 for r in rowsB)
    vA = ctl.verify(tagA, nA, 4)
    exits = crash_log("f4cA")
    lib.stop_worker("f4cA")
    lib.stop_server()
    lates = [r["late_s"] for r in [x for x in rowsA if x["kind"] == "approval_timeout"] + rowsB if r["late_s"] is not None]
    out = {"id": "F4c", "events": log, "worker_exit_records": exit_summary(exits), "control_early_check": early,
           "statuses_A": stA, "statuses_B": stB, "approval_exactly_once_and_correct_outcome": okA, "rollup_exactly_once": okB,
           "lateness_s_min_max_for_timer_driven": [min(lates), max(lates)] if lates else None, "verify_A_turns": vA,
           "granted": sum(r["kind"] == "approval_granted" for r in rowsA), "timed_out": sum(r["kind"] == "approval_timeout" for r in rowsA),
           "PASS": okA and okB and vA["PASS"] and stA == {"COMPLETED": nA} and stB == {"COMPLETED": nB}
           and early["approval_resolved"] == 0 and early["rollups"] == 0 and len(exits) == 3
           and all(e["exit_code"] in (137, -9, 9) for e in log if "server_abrupt" in e["what"] or "worker_exit" in e["what"])}
    lib.save(f"f4c{suffix()}", out)
    print(out["PASS"], lates and [min(lates), max(lates)], [e["exit_code"] for e in log])


# ------------------------------------------------------------------ F4d history bounds (+ naive control, overload)
async def f4d():
    variant = "v1n" if "--naive" in sys.argv else "v1"
    turns, rate, can_len = sizes((5400, 3.3, 0), (300, 15.0, 0))
    turns, rate, can_len = int(arg("--turns", turns)), arg("--rate", rate), int(arg("--can-len", can_len))
    lib.record(turns=turns, inject_rate_per_s=rate, can_len=can_len, variant=variant)
    lib.start_server("1.7.3", f"f4d{variant}{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f4dA", "venv-new", variant, "hostA", "f4d")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t0 = time.time()
    tag = T("k")
    tasks = await lib.launch(client, tag, 1, "f4d", stats, turns=turns, period=1 / rate, expected=turns, can_len=can_len)
    await asyncio.gather(*tasks)
    inject_s = time.time() - t0
    st, waited = await lib.wait_done(client, tag, 1, 900 if variant == "v1" else 60)
    wf_id = ctl.wid(ctl.sids(tag, 1)[0])
    runs = []
    async for e in client.list_workflows(f'WorkflowId = "{wf_id}"'):
        runs.append({"run": e.run_id[:8], "events": e.history_length, "status": e.status.name})
    res = await client.get_workflow_handle(wf_id).result() if st.get("COMPLETED") else None
    v = ctl.verify(tag, 1, turns)
    lib.stop_worker("f4dA")
    lib.stop_server()
    total_events = sum(r["events"] for r in runs)
    out = {"id": "F4d" + ("-control-naive" if variant == "v1n" else ""), "variant": variant, "wall_s": round(time.time() - t0, 1),
           "inject_s": round(inject_s, 1), "signals_acknowledged_by_server": stats["signaled"], "workflow_result": res,
           "workflow_status": st, "runs": len(runs), "continue_as_new_boundaries": len(runs) - 1,
           "max_run_history_events": max(r["events"] for r in runs), "total_history_events": total_events,
           "events_per_turn": round(total_events / turns, 2), "doc_limits": {"warn_events": 10240, "hard_events": 51200},
           "verify": v}
    if variant == "v1":
        out["PASS"] = v["PASS"] and res is not None and res["sig"] == stats["signaled"] and max(r["events"] for r in runs) < 10240 \
            and (len(runs) - 1 >= 20 or turns < 2000)
    else:
        out["control_failed_as_required"] = not v["PASS"]
    lib.save(f"f4d{'n' if variant == 'v1n' else ''}{suffix()}", out)
    print(variant, out.get("PASS", out.get("control_failed_as_required")), out["continue_as_new_boundaries"],
          out["max_run_history_events"], out["events_per_turn"])


# ------------------------------------------------------------------ F4a targeted windows and fence-off controls
async def f4t():
    mode = sys.argv[sys.argv.index("--mode") + 1]          # ok | brokenkeys | brokenresult
    n, turns, period = sizes((40, 20, 6), (4, 6, 1.5))
    lib.record(sessions=n, turns_each=turns, period_s=period, mode=mode, crash_in_effect_gap_p=0.02, crash_after_append_p=0.02)
    lib.start_server("1.7.3", f"f4t{mode}{suffix()}-{int(time.time())}.db", TPORT)
    env = {"ENGINE_EFFECT_GAP": "0.05", "ENGINE_CRASH_IN_GAP": "0.02", "ENGINE_CRASH_AFTER_APPEND": "0.02"}
    if mode == "brokenkeys":
        env["ENGINE_BROKEN_KEYS"] = "1"
    if mode == "brokenresult":
        env["ENGINE_BROKEN_RESULT"] = "1"
    name = f"f4t{mode}"
    lib.start_loop(name, "venv-old", "v1", "hostA", "f4t", env=env, seed=5)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T({"ok": "ta", "brokenkeys": "tb", "brokenresult": "tc"}[mode])
    tasks = await lib.launch(client, tag, n, "f4t", stats, turns=turns, period=period, expected=turns)
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 900)
    v = ctl.verify(tag, n, turns)
    rows = crash_log(name)
    lib.stop_worker(name)
    lib.stop_server()
    expect_pass = mode == "ok"
    out = {"id": "F4a-targeted-" + mode, "statuses": st, "exits": exit_summary(rows), "verify": v,
           "problems_sample": v["problems"][:4], "verify_PASS_expected": expect_pass, "as_expected": v["PASS"] == expect_pass}
    lib.save(f"f4t-{mode}{suffix()}", out)
    print(mode, "verify PASS:", v["PASS"], out["exits"], "expected:", expect_pass)


# ------------------------------------------------------------------ Postgres restart under load
async def f4pg():
    n, turns, period = sizes((40, 20, 6), (4, 6, 1.5))
    restarts = sizes((20, 55), (6, 14))
    lib.record(sessions=n, turns_each=turns, period_s=period, pg_restart_at_s=list(restarts))
    lib.start_server("1.7.3", f"f4pg{suffix()}-{int(time.time())}.db", TPORT)
    env = {"ENGINE_EFFECT_GAP": "0.05", "ENGINE_CRASH_IN_GAP": "0.01", "ENGINE_CRASH_AFTER_APPEND": "0.01"}
    name = "f4pgloop"
    lib.start_loop(name, "venv-old", "v1", "hostA", "f4pg", env=env, seed=9)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("pg")
    t0 = time.time()
    tasks = await lib.launch(client, tag, n, "f4pg", stats, turns=turns, period=period, expected=turns)
    restarts_done = []
    for at_s in restarts:
        await asyncio.sleep(max(0, at_s - (time.time() - t0)))
        t1 = time.time()
        r = subprocess.run(["docker", "restart", PG_CONTAINER], capture_output=True, text=True, timeout=120)
        restarts_done.append({"at_s": round(t1 - t0, 1), "took_s": round(time.time() - t1, 1), "rc": r.returncode})
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 900)
    v = ctl.verify(tag, n, turns)
    rows = crash_log(name)
    lib.stop_worker(name)
    lib.stop_server()
    out = {"id": "F4-postgres-restart", "pg_restarts": restarts_done, "exits": exit_summary(rows), "statuses": st, "driver": stats,
           "verify": v, "PASS": v["PASS"] and st == {"COMPLETED": n} and all(r["rc"] == 0 for r in restarts_done)}
    lib.save(f"f4pg{suffix()}", out)
    print(out["PASS"], restarts_done, out["exits"])


if __name__ == "__main__":
    asyncio.run({"f4a": f4a, "f4b": f4b, "f4c": f4c, "f4d": f4d, "f4t": f4t, "f4pg": f4pg}[sys.argv[1]]())
