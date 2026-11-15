"""F7 (engine upgrade) and F8 (engine independence drill): python -m engine.exps8 <f7|f7down|f8> [--small]"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

from . import ctl, keys, lib, pgstore, state
from .common import HOME, TENANT
from .exps import SMALL, T, TPORT, sizes, suffix


def rows(tag, n, kind):
    return {s: pgstore.events_of(s, kind) for s in ctl.sids(tag, n)}


def timer_check(tagP, nP, tagR, nR, roll_s, approved):
    """Exactly-once and lateness for approval waits and rollup timers (from Postgres events)."""
    a = []
    for s in ctl.sids(tagP, nP):
        req = pgstore.events_of(s, "approval_requested")
        res = pgstore.events_of(s, "approval_granted") + pgstore.events_of(s, "approval_timeout")
        fin = pgstore.events_of(s, "session_finished")
        dl = json.loads(req[0][3])["deadline_ms"] / 1000 if req else None
        a.append({"sid": s, "req": len(req), "res": len(res), "fin": len(fin), "kind": res[0][2] if res else None,
                  "late_s": round(res[0][4].timestamp() - dl, 1) if res and dl else None})
    r = []
    for s in ctl.sids(tagR, nR):
        ro = pgstore.events_of(s, "rollup")
        st = pgstore.events_of(s, "session_started")
        fin = pgstore.events_of(s, "session_finished")
        r.append({"sid": s, "rollups": len(ro), "fin": len(fin),
                  "late_s": round(ro[0][4].timestamp() - st[0][4].timestamp() - roll_s, 1) if ro and st else None})
    okA = all(x["req"] == 1 and x["res"] == 1 and x["fin"] == 1 for x in a)
    okA = okA and all((x["kind"] == "approval_granted") == (x["sid"] in approved) for x in a)
    okR = all(x["rollups"] == 1 and x["fin"] == 1 for x in r)
    lates = [x["late_s"] for x in [y for y in a if y["kind"] == "approval_timeout"] + r if x["late_s"] is not None]  # granted resolve early by design
    return {"approvals_exactly_once_and_correct": okA, "rollups_exactly_once": okR,
            "lateness_s_min_max": [min(lates), max(lates)] if lates else None,
            "granted": sum(x["kind"] == "approval_granted" for x in a), "timed_out": sum(x["kind"] == "approval_timeout" for x in a)}


async def approve_pg_and_signal(client, sids, with_pg=True):
    for s in sids:
        if with_pg:
            await asyncio.to_thread(pgstore.append_once, keys.approval_key(s, "grant"), s, "approval", "approval_grant", {},
                                    "approver", "user_typed")
        await client.get_workflow_handle(ctl.wid(s)).signal("approve", "g")


# ------------------------------------------------------------------ F7
async def f7():
    nA, turns, period, nP, nR, appr, roll, t_srv, t_sdk = sizes((80, 30, 10, 15, 15, 200, 150, 60, 170), (6, 8, 3, 3, 3, 40, 30, 12, 24))
    db = f"f7{suffix()}-{int(time.time())}.db"
    lib.start_server("1.7.3", db, TPORT)
    lib.start_worker("f7a", "venv-old", "v1", "sdk-1.31.0", "f7", env={"ENGINE_EFFECT_GAP": "0.03"})
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    A, P, R = T("up") + "a", T("up") + "b", T("up") + "c"
    t0 = time.time()
    tasks = await lib.launch(client, A, nA, "f7", stats, turns=turns, period=period, expected=turns)
    tasks += await lib.launch(client, P, nP, "f7", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr)
    tasks += await lib.launch(client, R, nR, "f7", stats, turns=0, rollup_s=roll, stop_after_rollups=1)
    approved = ctl.sids(P, nP)[: nP // 2]
    ev = []
    await asyncio.sleep(t_srv)
    # server upgrade 1.31.2 -> 1.32.2 on the same persistence (SQLite file) while sessions run and others are parked
    pending_before = lib.q("select count(*) from events where kind='turn_input' and session_id like %s", A + "%")[0][0]
    ts = time.time()
    lib.stop_server()
    lib.start_server("1.10.1", db, TPORT)
    ev.append({"t": round(ts - t0, 1), "what": "server 1.31.2 -> 1.32.2 on same sqlite file", "downtime_s": round(time.time() - ts, 1)})
    await asyncio.sleep(max(0, t_sdk / 2 - (time.time() - t0)))
    await approve_pg_and_signal(client, approved)
    ev.append({"t": round(time.time() - t0, 1), "what": "approved half of the parked approval sessions"})
    await asyncio.sleep(max(0, t_sdk - (time.time() - t0)))
    ts = time.time()
    lib.stop_worker("f7a")
    lib.start_worker("f7b", "venv-new", "v1", "sdk-1.36.1", "f7", env={"ENGINE_EFFECT_GAP": "0.03"})
    ev.append({"t": round(ts - t0, 1), "what": "SDK 1.31.0 -> 1.36.1 (worker swap)", "gap_s": round(time.time() - ts, 1)})
    await asyncio.gather(*tasks)
    stA, _ = await lib.wait_done(client, A, nA, 900)
    stP, _ = await lib.wait_done(client, P, nP, 900)
    stR, _ = await lib.wait_done(client, R, nR, 900)
    vA, vP = ctl.verify(A, nA, turns), ctl.verify(P, nP, 4)
    tm = timer_check(P, nP, R, nR, roll, approved)
    calls = lib.q("select worker, count(*) from ext_calls where key like %s group by 1", f"eff/{TENANT}/{T('up')}%")
    lib.stop_worker("f7b")
    lib.stop_server()
    shutil.copy(HOME / "run" / db, HOME / "run" / (db + ".post-upgrade"))
    srvlog = sorted((HOME / "logs").glob(f"server-{TPORT}-1.10.1-*.log"))[-1]
    out = {"id": "F7", "from": {"server": "1.31.2 (CLI 1.7.3)", "sdk": "temporalio 1.31.0"},
           "to": {"server": "1.32.2 (CLI 1.10.1)", "sdk": "temporalio 1.36.1"}, "events": ev,
           "persistence": "SQLite file run/%s shared by both CLI versions (the dev server migrates it on start); not the documented production schema-tool path" % db,
           "statuses": [stA, stP, stR], "verify_A": vA, "verify_P": vP, "timers": tm, "external_calls_by_worker": [list(map(str, r)) for r in calls],
           "new_server_log_head": srvlog.read_text()[:600], "driver": stats,
           "PASS": vA["PASS"] and vP["PASS"] and tm["approvals_exactly_once_and_correct"] and tm["rollups_exactly_once"]
           and stA == {"COMPLETED": nA} and stP == {"COMPLETED": nP} and stR == {"COMPLETED": nR}
           and {r[0] for r in calls} == {"sdk-1.31.0", "sdk-1.36.1"}}
    lib.save(f"f7{suffix()}", out)
    print(out["PASS"], ev, tm)


async def f7down():
    """Control: the upgraded persistence cannot be opened by the older server (the upgrade really migrated state)."""
    db = sorted((HOME / "run").glob("f7-*.db.post-upgrade"))[-1]
    work = HOME / "run" / "f7-downgrade-attempt.db"
    shutil.copy(db, work)
    p = lib.start_server_try("1.7.3", work.name, TPORT)
    print(json.dumps(p))


# ------------------------------------------------------------------ F8 engine independence
def reconstruct(sid, lossy=False, now_ms=None):
    """Rebuild a session's carried state from Postgres events ALONE (the engine is gone)."""
    ev = pgstore.events_of(sid)
    started = [e for e in ev if e[2] == "session_started"]
    if not started or any(e[2] == "session_finished" for e in ev):
        return None
    p = json.loads(started[0][3])["params"]
    st = state.new_state(TENANT, sid, ver=1, **p)
    st["turns_done"] = sum(1 for e in ev if e[2] == "turn_result")
    ro = [e for e in ev if e[2] == "rollup"]
    st["rollups"] = len(ro)
    if p["rollup_s"]:
        last = (ro[-1][4] if ro else started[0][4]).timestamp()
        st["rollup_deadline_ms"] = 0 if lossy else int((last + p["rollup_s"]) * 1000)
    req = [e for e in ev if e[2] == "approval_requested"]
    res = [e for e in ev if e[2] in ("approval_granted", "approval_timeout")]
    if res:
        st["approval"] = res[0][2]
    elif req:
        st["approval"] = "waiting"
        st["approval_deadline_ms"] = (now_ms + int(p["approval_to_s"] * 1000)) if lossy else json.loads(req[0][3])["deadline_ms"]
    return st


def fin(tag, n):
    return sum(1 for s in ctl.sids(tag, n) if pgstore.events_of(s, 'session_finished'))


async def f8():
    nA, turns, period, nP, nR, nX, nL, appr, roll, t_cut = sizes((60, 24, 8, 10, 10, 6, 6, 150, 120, 70), (6, 10, 2, 3, 3, 2, 2, 50, 40, 14))
    lib.start_server("1.7.3", f"f8a{suffix()}-{int(time.time())}.db", TPORT)
    dbA = sorted((HOME / "run").glob("f8a*.db"))[-1]
    lib.start_worker("f8w1", "venv-new", "v1", "engine-1", "f8")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    A, P, R, X, L = (T("h") + c for c in "abcde")
    t0 = time.time()
    tasks = await lib.launch(client, A, nA, "f8", stats, turns=turns, period=period, expected=turns)
    for tag, n in ((P, nP), (X, nX), (L, nL)):
        tasks += await lib.launch(client, tag, n, "f8", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr)
    tasks += await lib.launch(client, R, nR, "f8", stats, turns=0, rollup_s=roll, stop_after_rollups=1)
    approvedP = ctl.sids(P, nP)[: nP // 2]
    approvedX = ctl.sids(X, nX)
    await asyncio.sleep(10)
    await approve_pg_and_signal(client, approvedP, with_pg=True)          # grant is an event in Postgres, then a doorbell
    await approve_pg_and_signal(client, approvedX, with_pg=False)         # grant exists only as a signal inside the engine
    await asyncio.sleep(max(0, t_cut - (time.time() - t0)))
    # DISCARD THE ENGINE: worker gone, server gone, its persistence deleted. Nothing but Postgres remains.
    cut = time.time()
    lib.stop_worker("f8w1", hard=True)
    lib.stop_server(hard=True)
    os.remove(dbA)
    for f in HOME.joinpath("run").glob(dbA.name + "-*"):
        os.remove(f)
    time.sleep(6)
    lib.start_server("1.10.1", f"f8b{suffix()}-{int(time.time())}.db", TPORT)   # fresh server, empty persistence (also a newer release)
    lib.start_worker("f8w2", "venv-new", "v1", "engine-2", "f8")
    client2, _ = await lib.connect_retry("orch2")
    now_ms = int(time.time() * 1000)
    rebuilt, skipped, lossy_n = 0, 0, 0
    t_reb = time.time()
    for tag, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL)):
        for s in ctl.sids(tag, n):
            st = await asyncio.to_thread(reconstruct, s, tag == L, now_ms)
            if st is None:
                skipped += 1
                continue
            await client2.start_workflow("SessionWorkflow", st, id=ctl.wid(s), task_queue="f8",
                                         id_conflict_policy=__import__("temporalio.common", fromlist=["x"]).WorkflowIDConflictPolicy.USE_EXISTING)
            rebuilt += 1
            lossy_n += tag == L
    reb_s = round(time.time() - t_reb, 1)
    await asyncio.gather(*tasks)
    out_st = {}
    for tag, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL)):
        out_st[tag] = (await lib.wait_done(client2, tag, n, 900))[0]
    vA, vP, vX, vL = ctl.verify(A, nA, turns), ctl.verify(P, nP, 4), ctl.verify(X, nX, 4), ctl.verify(L, nL, 4)
    tmP = timer_check(P, nP, R, nR, roll, approvedP)
    tmX = timer_check(X, nX, R, nR, roll, approvedX)
    tmL = timer_check(L, nL, R, nR, roll, [])
    lib.stop_worker("f8w2")
    lib.stop_server()
    dropped = stats.get("doorbell_dropped", 0)
    out = {"id": "F8", "engine_discarded_at_s": round(cut - t0, 1), "sessions": nA + nP + nR + nX + nL, "rebuilt": rebuilt, "already_finished": skipped,
           "rebuild_wall_s": reb_s, "new_engine": "fresh server 1.32.2 (CLI 1.10.1), empty SQLite, new worker identity; old SQLite file deleted",
           "doorbells_dropped_by_new_engine_not_found": dropped, "statuses": out_st, "finished_in_postgres": {t: fin(t, n) for t, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL))},
           "verify_A": vA, "verify_P": vP, "timers_main_groups": tmP,
           "stored_in_postgres_and_used": ["session_started.params (expected_turns, timers, can_len)", "turn_input events (unprocessed = no effects row res/<id>)",
                                          "turn_result and rollup events (counters and rollup deadline = last rollup ts + interval)",
                                          "approval_requested.deadline_ms", "approval_grant written BEFORE the doorbell signal"],
           "loss_if_not_stored": {
               "grant_only_as_signal_X": {"verify": vX["PASS"], "outcomes": tmX, "note": "grant existed only inside the old engine; the rebuilt session times out although the human approved"},
               "deadline_not_stored_L": {"outcomes": tmL, "note": "rebuilt with timers restarted from the rebuild time; lateness is the loss"}},
           "PASS": vA["PASS"] and vP["PASS"] and tmP["approvals_exactly_once_and_correct"] and tmP["rollups_exactly_once"]
           and all(fin(t, n) == n for t, n in ((A, nA), (P, nP), (R, nR)))
           and not any(k in ("RUNNING", "FAILED", "TERMINATED", "TIMED_OUT") for t in (A, P, R) for k in out_st[t])}
    lib.save(f"f8{suffix()}", out)
    print(out["PASS"], rebuilt, skipped, tmP, tmX, tmL)


if __name__ == "__main__":
    asyncio.run({"f7": f7, "f7down": f7down, "f8": f8}[sys.argv[1]]())
