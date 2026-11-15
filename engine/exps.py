"""F4 experiments (run on t2-dev under venv-new or venv-old: python -m engine.exps <name> [--small] [--tport N]).
Every PASS has a control that can fail; controls are produced by the same run or by a fence-off run."""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import time

from temporalio.client import WorkflowExecutionStatus

from . import ctl, keys, lib, pgstore
from .common import HOME, TENANT
from .worker import connect

SMALL = "--small" in sys.argv
TPORT = int(sys.argv[sys.argv.index("--tport") + 1]) if "--tport" in sys.argv else 0   # 0 = pick a free port block

TPORT = TPORT or lib.pick_port()


def sizes(full, small):
    return small if SMALL else full


RETAG = sys.argv[sys.argv.index("--retag") + 1] if "--retag" in sys.argv else ""


def T(t):
    t = RETAG + t
    return t.upper() if SMALL else t


def suffix():
    return ("-small" if SMALL else "") + (f"-{RETAG}" if RETAG else "")


# ------------------------------------------------------------------ F4b host move
async def f4b():
    n, turns, period, move_at = sizes((100, 20, 15, 60), (10, 6, 3, 10))
    lib.start_server("1.7.3", f"f4b{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f4bA", "venv-old", "v1", "hostA", "f4b", env={"ENGINE_EFFECT_GAP": "0.05"})
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("p")
    tasks = await lib.launch(client, tag, n, "f4b", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(move_at)
    rcA = lib.stop_worker("f4bA", hard=True)
    # a different host: other working dir, other ENGINE_HOME with its own copy of the connection files, other identity
    hb = HOME / "hostB"
    if hb.exists():
        shutil.rmtree(hb)
    (hb / "code").mkdir(parents=True)
    shutil.copytree(HOME / "code/engine", hb / "code/engine")
    (hb / "pg").mkdir()
    for f in ("pg_port", "pgpass"):
        shutil.copy(HOME / "pg" / f, hb / "pg" / f)
    os.chmod(hb / "pg/pgpass", 0o600)
    time.sleep(8)
    # control: with no worker polling, the verifier must report the run as incomplete
    early = ctl.verify(tag, n, turns)
    early_calls = lib.q("select worker, count(*) from ext_calls where key like %s group by 1", f"eff/{TENANT}/{tag}%")
    lib.start_worker("f4bB", "venv-old", "v1", "hostB", "f4b", cwd=hb / "code", home=hb,
                     env={"ENGINE_EFFECT_GAP": "0.05"})
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 900)
    final = ctl.verify(tag, n, turns)
    calls = lib.q("select worker, count(*) from ext_calls where key like %s group by 1", f"eff/{TENANT}/{tag}%")
    lib.stop_worker("f4bB")
    lib.stop_server()
    out = {"id": "F4b", "sessions": n, "turns": turns, "sdk": "temporalio 1.31.0", "server": "1.31.2 (CLI 1.7.3)",
           "worker_A_exit": rcA, "move_at_s": move_at, "gap_s": 8, "workflow_statuses": st, "wait_s": waited,
           "control_early_verify_PASS_should_be_False": early["PASS"], "control_early_problems": len(early["problems"]),
           "external_calls_by_worker": [list(map(str, r)) for r in calls],
           "external_calls_by_worker_at_gap": [list(map(str, r)) for r in early_calls], "verify": final,
           "driver": stats, "PASS": final["PASS"] and st == {"COMPLETED": n} and not early["PASS"]
           and any(r[0] == "hostB" for r in calls)}
    print(json.dumps(lib.save(f"f4b{suffix()}", out) and out["PASS"]))


# ------------------------------------------------------------------ F4c long waits
def event_rows(sid, kind):
    return pgstore.events_of(sid, kind)


async def f4c():
    nA, nB, appr_to, roll_s = sizes((30, 30, 240, 180), (4, 4, 40, 30))
    DBC = f"f4c{suffix()}-{int(time.time())}.db"
    lib.start_server("1.7.3", DBC, TPORT)
    lib.start_worker("f4cA", "venv-new", "v1", "hostA", "f4c")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t0 = time.time()
    tA = await lib.launch(client, T("r"), nA, "f4c", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr_to)
    tB = await lib.launch(client, T("s"), nB, "f4c", stats, turns=0, rollup_s=roll_s, stop_after_rollups=1)
    await asyncio.gather(*tA)
    log = []

    async def at(t, what, fn):
        d = t - (time.time() - t0)
        if d > 0:
            await asyncio.sleep(d)
        await fn()
        log.append({"t": round(time.time() - t0, 1), "what": what})

    sc = (appr_to / 240.0)
    # control: early in the wait nothing has resolved, and the check must say so
    early = {}

    async def early_check():
        early["approval_resolved"] = sum(len(event_rows(s, "approval_timeout")) + len(event_rows(s, "approval_granted"))
                                         for s in ctl.sids(T("r"), nA))
        early["rollups"] = sum(len(event_rows(s, "rollup")) for s in ctl.sids(T("s"), nB))

    async def crash(name):
        lib.stop_worker(name, hard=True)
        await asyncio.sleep(3)
        lib.start_worker(name, "venv-new", "v1", "hostA", "f4c")

    async def server_hard():
        lib.stop_server(hard=True)
        await asyncio.sleep(5)
        lib.start_server("1.7.3", DBC, TPORT)

    async def server_soft():
        lib.stop_server()
        await asyncio.sleep(3)
        lib.start_server("1.7.3", DBC, TPORT)

    approved = ctl.sids(T("r"), nA)[: nA // 2]

    async def approve():
        for s in approved:
            await asyncio.to_thread(pgstore.append_once, keys.approval_key(s, "grant"), s, "approval", "approval_grant",
                                    {}, "approver", "user_typed")
            await client.get_workflow_handle(ctl.wid(s)).signal("approve", T("r"))

    await at(20 * sc + 5, "early_check", early_check)
    await at(30 * sc, "worker_crash_1", lambda: crash("f4cA"))
    await at(60 * sc, "server_hard_stop_restart", server_hard)
    await at(95 * sc, "worker_crash_2", lambda: crash("f4cA"))
    await at(120 * sc, "approve_half", approve)
    await at(150 * sc, "server_graceful_restart", server_soft)
    await at(170 * sc, "worker_crash_3", lambda: crash("f4cA"))
    stA, wA = await lib.wait_done(client, T("r"), nA, appr_to * 3)
    stB, wB = await lib.wait_done(client, T("s"), nB, roll_s * 3)
    # evidence
    rowsA = []
    for s in ctl.sids(T("r"), nA):
        req = event_rows(s, "approval_requested")
        res = event_rows(s, "approval_granted") + event_rows(s, "approval_timeout")
        fin = event_rows(s, "session_finished")
        dl = json.loads(req[0][3])["deadline_ms"] / 1000 if req else None
        rowsA.append({"sid": s, "requests": len(req), "resolutions": len(res), "kind": res[0][2] if res else None,
                      "finished": len(fin), "late_s": round(res[0][4].timestamp() - dl, 2) if res and dl else None})
    rowsB = []
    for s in ctl.sids(T("s"), nB):
        ro = event_rows(s, "rollup")
        st_ev = event_rows(s, "session_started")
        fin = event_rows(s, "session_finished")
        rowsB.append({"sid": s, "rollups": len(ro), "finished": len(fin),
                      "late_s": round(ro[0][4].timestamp() - st_ev[0][4].timestamp() - roll_s, 2) if ro and st_ev else None})
    okA = all(r["requests"] == 1 and r["resolutions"] == 1 and r["finished"] == 1 for r in rowsA)
    okA = okA and all((r["kind"] == "approval_granted") == (r["sid"] in approved) for r in rowsA)
    okB = all(r["rollups"] == 1 and r["finished"] == 1 for r in rowsB)
    vA = ctl.verify(T("r"), nA, 4)
    lib.stop_worker("f4cA")
    lib.stop_server()
    lates = [r["late_s"] for r in [x for x in rowsA if x["kind"] == "approval_timeout"] + rowsB if r["late_s"] is not None]  # granted ones resolve early by design
    out = {"id": "F4c", "approval_sessions": nA, "rollup_sessions": nB, "approval_timeout_s": appr_to, "rollup_s": roll_s,
           "events": log, "control_early_check": early, "statuses_A": stA, "statuses_B": stB,
           "approval_exactly_once_and_correct_outcome": okA, "rollup_exactly_once": okB,
           "lateness_s_min_max": [min(lates), max(lates)] if lates else None, "verify_A_turns": vA,
           "rowsA_sample": rowsA[:3], "rowsB_sample": rowsB[:3], "granted": sum(r["kind"] == "approval_granted" for r in rowsA),
           "timed_out": sum(r["kind"] == "approval_timeout" for r in rowsA),
           "PASS": okA and okB and vA["PASS"] and stA == {"COMPLETED": nA} and stB == {"COMPLETED": nB}
           and early["approval_resolved"] == 0 and early["rollups"] == 0}
    lib.save(f"f4c{suffix()}", out)
    print(out["PASS"], lates and [min(lates), max(lates)])


# ------------------------------------------------------------------ F4d history bounds
async def f4d():
    variant = "v1n" if "--naive" in sys.argv else "v1"
    turns, rate, can_len = sizes((6000, 8.0, 0), (300, 15.0, 0))
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])
    if "--rate" in sys.argv:
        rate = float(sys.argv[sys.argv.index("--rate") + 1])
    if "--can-len" in sys.argv:
        can_len = int(sys.argv[sys.argv.index("--can-len") + 1])
    lib.start_server("1.7.3", f"f4d{variant}{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f4dA", "venv-new", variant, "hostA", "f4d")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t0 = time.time()
    tag = T("k") if variant == "v1" else T("n")
    tasks = await lib.launch(client, tag, 1, "f4d", stats, turns=turns, period=1 / rate, expected=turns, can_len=can_len)
    await asyncio.gather(*tasks)
    sent = stats["signaled"]
    st, waited = await lib.wait_done(client, tag, 1, 600 if variant == "v1" else 60)
    wf_id = ctl.wid(ctl.sids(tag, 1)[0])
    runs = []
    async for e in client.list_workflows(f'WorkflowId = "{wf_id}"'):
        runs.append({"run": e.run_id[:8], "events": e.history_length, "status": e.status.name})
    res = None
    if st.get("COMPLETED"):
        res = await client.get_workflow_handle(wf_id).result()
    v = ctl.verify(tag, 1, turns)
    with lib.pg_connect() as c:
        done = c.execute("select count(*) from events where session_id=%s and kind='turn_result'", (ctl.sids(tag, 1)[0],)).fetchone()[0]
    lib.stop_worker("f4dA")
    lib.stop_server()
    total_events = sum(r["events"] for r in runs)
    out = {"id": "F4d" + ("-control-naive" if variant == "v1n" else ""), "variant": variant, "turns": turns,
           "inject_rate_per_s": rate, "wall_s": round(time.time() - t0, 1), "signals_sent": sent, "result": res,
           "workflow_status": st, "runs": len(runs), "continue_as_new_boundaries": len(runs) - 1,
           "max_run_history_events": max(r["events"] for r in runs), "total_history_events": total_events,
           "events_per_turn": round(total_events / turns, 2), "doc_limits": {"warn_events": 10240, "hard_events": 51200},
           "results_in_postgres": done, "verify": v,
           "PASS": variant == "v1" and v["PASS"] and res is not None and res["sig"] == sent and len(runs) - 1 >= 20
           and max(r["events"] for r in runs) < 10240}
    lib.save(f"f4d{'n' if variant == 'v1n' else ''}{suffix()}", out)
    print(out["PASS"], out["continue_as_new_boundaries"], out["max_run_history_events"], out["events_per_turn"])


# ------------------------------------------------------------------ F4a targeted crashes (+ fence-off controls)
async def f4t():
    mode = sys.argv[sys.argv.index("--mode") + 1]          # ok | brokenkeys | brokenresult
    n, turns, period = sizes((40, 20, 6), (4, 6, 1.5))
    lib.start_server("1.7.3", f"f4t{mode}{suffix()}-{int(time.time())}.db", TPORT)
    env = {"ENGINE_EFFECT_GAP": "0.05", "ENGINE_CRASH_IN_GAP": "0.02", "ENGINE_CRASH_AFTER_APPEND": "0.02"}
    if mode == "brokenkeys":
        env["ENGINE_BROKEN_KEYS"] = "1"
    if mode == "brokenresult":
        env["ENGINE_BROKEN_RESULT"] = "1"
    name = f"f4t{mode}"
    (HOME / "logs" / f"{name}.crashes").write_text("")
    lib.start_loop(name, "venv-old", "v1", "hostA", "f4t", env=env, seed=5)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T({"ok": "ta", "brokenkeys": "tb", "brokenresult": "tc"}[mode])
    tasks = await lib.launch(client, tag, n, "f4t", stats, turns=turns, period=period, expected=turns)
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 900)
    v = ctl.verify(tag, n, turns)
    crashes = [json.loads(l) for l in open(HOME / "logs" / f"{name}.crashes") if l.strip()]
    lib.stop_worker(name)
    lib.stop_server()
    by = {}
    for c in crashes:
        by[c.get("where", "timed")] = by.get(c.get("where", "timed"), 0) + 1
    expect_pass = mode == "ok"
    out = {"id": "F4a-targeted-" + mode, "sessions": n, "turns": turns, "crashes_total": len(crashes), "crashes_by_window": by,
           "statuses": st, "verify": v, "problems_sample": v["problems"][:4], "verify_PASS_expected": expect_pass,
           "as_expected": v["PASS"] == expect_pass}
    lib.save(f"f4t-{mode}{suffix()}", out)
    print(mode, "verify PASS:", v["PASS"], "crashes:", by, "expected:", expect_pass)


if __name__ == "__main__":
    asyncio.run({"f4b": f4b, "f4c": f4c, "f4d": f4d, "f4t": f4t}[sys.argv[1]]())
