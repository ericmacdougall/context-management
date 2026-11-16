"""F7 (engine upgrade, with a fresh-database control) and F8 (engine independence drill):
python -m engine.exps8 <f7|f7ctl|f8> [--small]. Results carry their provenance; parameters are read from the run."""
import asyncio
import json
import os
import sys
import time

from . import ctl, lib, pgstore, state
from .common import DEMO_TENANT as TENANT
from .common import HOME
from .exps import T, TPORT, arg, sizes, suffix


def timer_check(tagP, nP, tagR, nR, roll_s, approved):
    """Exactly-once and latency for approval waits and rollup timers, from Postgres events. Lateness is measured for
    timer-driven resolutions (timeouts, rollups); for GRANTED approvals the figure is grant-to-resolution latency."""
    a = []
    for s in ctl.sids(tagP, nP):
        req = lib.events_of(s, "approval_requested")
        res = lib.events_of(s, "approval_granted") + lib.events_of(s, "approval_timeout")
        grants = lib.events_of(s, "approval_grant")
        fin = lib.events_of(s, "session_finished")
        dl = json.loads(req[0][3])["deadline_ms"] / 1000 if req else None
        kind = res[0][2] if res else None
        row = {"sid": s, "req": len(req), "res": len(res), "fin": len(fin), "kind": kind, "late_s": None, "grant_latency_s": None}
        if res and kind == "approval_timeout" and dl:
            row["late_s"] = round(res[0][4].timestamp() - dl, 1)
        if res and kind == "approval_granted" and grants:
            row["grant_latency_s"] = round(res[0][4].timestamp() - grants[0][4].timestamp(), 1)
        a.append(row)
    r = []
    for s in ctl.sids(tagR, nR):
        ro, st, fin = lib.events_of(s, "rollup"), lib.events_of(s, "session_started"), lib.events_of(s, "session_finished")
        r.append({"sid": s, "rollups": len(ro), "fin": len(fin),
                  "late_s": round(ro[0][4].timestamp() - st[0][4].timestamp() - roll_s, 1) if ro and st else None})
    okA = all(x["req"] == 1 and x["res"] == 1 and x["fin"] == 1 for x in a)
    okA = okA and all((x["kind"] == "approval_granted") == (x["sid"] in approved) for x in a)
    okR = all(x["rollups"] == 1 and x["fin"] == 1 for x in r)
    lates = [x["late_s"] for x in a + r if x["late_s"] is not None]
    lat = [x["grant_latency_s"] for x in a if x["grant_latency_s"] is not None]
    return {"approvals_exactly_once_and_correct": okA, "rollups_exactly_once": okR,
            "timer_lateness_s_min_max": [min(lates), max(lates)] if lates else None,
            "granted_resolution_latency_s_min_max": [min(lat), max(lat)] if lat else None,
            "granted": sum(x["kind"] == "approval_granted" for x in a), "timed_out": sum(x["kind"] == "approval_timeout" for x in a)}


# ------------------------------------------------------------------ F7
async def f7():
    nA, turns, period, nP, nR, appr, roll, t_srv, t_sdk = sizes((80, 30, 10, 15, 15, 200, 150, 60, 170), (6, 8, 3, 3, 3, 40, 30, 12, 24))
    lib.record(busy_sessions=nA, turns_each=turns, period_s=period, approval_sessions=nP, rollup_sessions=nR,
               approval_timeout_s=appr, rollup_s=roll, server_upgrade_at_s=t_srv, sdk_swap_at_s=t_sdk)
    db = f"f7{suffix()}-{int(time.time())}.db"
    pg_old, pg_new = arg("--pg-old", "1.31.5", str), arg("--pg-new", "1.32.2", str)
    if lib.BACKEND == "pg":
        lib.set_pg_versions(pg_old, pg_new)
        lib.record(server_from=pg_old, server_to=pg_new)
    lib.start_server("1.7.3", db, TPORT)
    lib.start_worker("f7a", "venv-old", "v1", "sdk-1.31.0", "f7", env={"ENGINE_EFFECT_GAP": "0.03"})
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    A, P, R = T("ua"), T("ub"), T("uc")
    t0 = time.time()
    tasks = await lib.launch(client, A, nA, "f7", stats, turns=turns, period=period, expected=turns)
    tasks += await lib.launch(client, P, nP, "f7", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr)
    tasks += await lib.launch(client, R, nR, "f7", stats, turns=0, rollup_s=roll, stop_after_rollups=1)
    approved = ctl.sids(P, nP)[: nP // 2]
    ev = []
    await asyncio.sleep(t_srv)
    ts = time.time()
    lib.stop_server()
    lib.start_server("1.10.1", db, TPORT)      # same persistence file
    ev.append({"t": round(ts - t0, 1), "what": (f"server {pg_old} -> {pg_new}: temporal-sql-tool update-schema, then the new server" if lib.BACKEND == "pg"
                                                  else "server 1.31.2 -> 1.32.2 on the same sqlite file"), "downtime_s": round(time.time() - ts, 1)})
    await asyncio.sleep(max(0, t_sdk / 2 - (time.time() - t0)))
    grants = [await ctl.approve_session(client, s, TENANT) for s in approved]
    ev.append({"t": round(time.time() - t0, 1), "what": "approved half of the parked approval sessions", "grant_results": {g: grants.count(g) for g in set(grants)}})
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
    calls = lib.q("select worker, count(*) from ext_calls where tenant_id=%s and key like %s escape '\\' group by 1",
                  TENANT, f"{TENANT}/eff/{TENANT}/" + ctl.like(A))
    lib.stop_worker("f7b")
    lib.stop_server()
    text = lib.server_log_text(TPORT, "1.10.1")
    srv_from, srv_to = ((pg_old, pg_new) if lib.BACKEND == "pg" else ("1.31.2 (CLI 1.7.3)", "1.32.2 (CLI 1.10.1)"))
    out = {"id": "F7", "from": {"server": srv_from, "sdk": "temporalio 1.31.0"},
           "to": {"server": srv_to, "sdk": "temporalio 1.36.1"}, "events": ev,
           "persistence": (f"Postgres persistence, schema updated with temporal-sql-tool of the target version before the server started "
                           "(see provenance.recorded.servers[*].schema_before/schema_after)" if lib.BACKEND == "pg" else
                           f"SQLite file run/{db} opened by both CLI versions; not the documented schema-tool path"),
           "statuses": [stA, stP, stR], "verify_A": vA, "verify_P": vP, "timers": tm,
           "external_calls_by_worker": {r[0]: r[1] for r in calls},
           "new_server_log_error_lines": sum(1 for l in text.splitlines() if "level=ERROR" in l),
           "new_server_log_error_samples": [l[:200] for l in text.splitlines() if "level=ERROR" in l][:3], "driver": stats,
           "PASS": vA["PASS"] and vP["PASS"] and tm["approvals_exactly_once_and_correct"] and tm["rollups_exactly_once"]
           and stA == {"COMPLETED": nA} and stP == {"COMPLETED": nP} and stR == {"COMPLETED": nR}
           and set(r[0] for r in calls) == {"sdk-1.31.0", "sdk-1.36.1"}}
    lib.save(f"f7{suffix()}", out)
    print(out["PASS"], ev, tm)


async def f7ctl():
    """Control for F7: if the persistence is NOT carried across the upgrade (new server, fresh database), the run must
    fail its verification (sessions are gone and nothing resumes them)."""
    n, turns, period, t_srv = sizes((20, 8, 3, 14), (4, 6, 1.5, 8))
    lib.record(sessions=n, turns_each=turns, period_s=period, upgrade_at_s=t_srv)
    lib.start_server("1.7.3", f"f7ctl{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f7c1", "venv-old", "v1", "sdk-1.31.0", "f7c")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("ud")
    tasks = await lib.launch(client, tag, n, "f7c", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(t_srv)
    lib.stop_server()
    lib.start_server("1.10.1", f"f7ctl-fresh{suffix()}-{int(time.time())}.db", TPORT)     # persistence NOT carried
    lib.start_worker("f7c2", "venv-new", "v1", "sdk-1.36.1", "f7c")
    await asyncio.gather(*tasks)
    await asyncio.sleep(20)
    st = await lib.statuses(client, tag, n)
    v = ctl.verify(tag, n, turns)
    lib.stop_worker("f7c1")
    lib.stop_worker("f7c2")
    lib.stop_server()
    out = {"id": "F7-control-fresh-database", "statuses": {k: list(st.values()).count(k) for k in set(st.values())}, "driver": stats,
           "verify": v, "control_failed_as_required": not v["PASS"]}
    lib.save(f"f7ctl{suffix()}", out)
    print("control failed as required:", out["control_failed_as_required"], out["statuses"])


# ------------------------------------------------------------------ F8 engine independence
def fin(tag, n):
    return sum(1 for s in ctl.sids(tag, n) if lib.events_of(s, "session_finished"))


def reconstruct(sid, lossy=False, now_ms=None):
    """Rebuild a session's carried state from Postgres events ALONE (the engine is gone)."""
    ev = lib.events_of(sid)
    started = [e for e in ev if e[2] == "session_started"]
    if not started or any(e[2] == "session_finished" for e in ev):
        return None
    p = json.loads(started[0][3])["params"]
    st = state.new_state(TENANT, sid, ver=1, **p)
    results = [e for e in ev if e[2] == "turn_result"]
    st["turns_done"] = len(results) + sum(1 for e in ev if e[2] == "turn_dead_letter")
    st["last_eid"] = json.loads(results[-1][3])["in"] if results else ""
    ro = [e for e in ev if e[2] == "rollup"]
    st["rollups"] = len(ro)
    if p["rollup_s"]:
        base = json.loads(ro[-1][3])["deadline_ms"] if ro else int(started[0][4].timestamp() * 1000)
        st["rollup_deadline_ms"] = 0 if lossy else base + int(p["rollup_s"] * 1000)
    req = [e for e in ev if e[2] == "approval_requested"]
    if req:
        rid = req[-1][1]
        res = [e for e in ev if e[2] in ("approval_granted", "approval_timeout") and json.loads(e[3]).get("request") == rid]
        if res:
            st["approval"] = res[0][2]
        else:
            st["approval"], st["approval_req"] = "waiting", rid
            st["approval_deadline_ms"] = (now_ms + int(p["approval_to_s"] * 1000)) if lossy else json.loads(req[-1][3])["deadline_ms"]
    return st


async def f8():
    nA, turns, period, nP, nR, nX, nL, appr, roll, t_cut = sizes((60, 24, 8, 10, 10, 6, 6, 150, 120, 70), (6, 10, 2, 4, 3, 2, 2, 50, 40, 14))
    lib.record(busy_sessions=nA, turns_each=turns, period_s=period, approval_sessions=nP, rollup_sessions=nR,
               grant_signal_only_sessions=nX, unstored_deadline_sessions=nL, approval_timeout_s=appr, rollup_s=roll, cut_at_s=t_cut)
    lib.start_server("1.7.3", f"f8a{suffix()}-{int(time.time())}.db", TPORT)
    dbA = sorted((HOME / "run").glob("f8a*.db"))[-1] if lib.BACKEND != "pg" else None
    lib.start_worker("f8w1", "venv-new", "v1", "engine-1", "f8")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    A, P, R, X, L = (T("h") + c for c in "abcde")
    t0 = time.time()
    tasks = await lib.launch(client, A, nA, "f8", stats, turns=turns, period=period, expected=turns)
    for tag, n in ((P, nP), (X, nX), (L, nL)):
        tasks += await lib.launch(client, tag, n, "f8", stats, turns=4, period=0.5, expected=4, approval_at=2, approval_to_s=appr)
    tasks += await lib.launch(client, R, nR, "f8", stats, turns=0, rollup_s=roll, stop_after_rollups=1)
    allP = ctl.sids(P, nP)
    approvedP = allP[: nP // 2 - 1]
    lost_doorbell = allP[nP // 2 - 1: nP // 2 + 1]
    approvedX = ctl.sids(X, nX)
    await asyncio.sleep(10)
    gP = [await ctl.approve_session(client, s, TENANT) for s in approvedP]                    # grant in Postgres first, then the doorbell
    gX = [await ctl.approve_session(client, s, TENANT, with_pg=False) for s in approvedX]     # doorbell only
    lost_results = []
    for s in lost_doorbell:                                    # granted in Postgres, doorbell lost, engine about to be discarded
        lost_results.append(pgstore.grant_approval(TENANT, s, await ctl.wait_request(TENANT, s), "approver-1"))
    approvedP = approvedP + lost_doorbell
    await asyncio.sleep(max(0, t_cut - (time.time() - t0)))
    cut = time.time()
    lib.stop_worker("f8w1", hard=True)       # DISCARD THE ENGINE: worker gone, server gone, its persistence deleted
    lib.stop_server(hard=True)
    if lib.BACKEND == "pg":
        discarded_db = lib.tpg_discard()          # the engine's persistence is dropped; only Postgres (the engine's) remains
    else:
        os.remove(dbA)
        for f in HOME.joinpath("run").glob(dbA.name + "-*"):
            os.remove(f)
    time.sleep(6)
    lib.start_server("1.10.1", f"f8b{suffix()}-{int(time.time())}.db", TPORT)    # fresh server, empty persistence
    lib.start_worker("f8w2", "venv-new", "v1", "engine-2", "f8")
    client2, _ = await lib.connect_retry("orch2")
    now_ms = int(time.time() * 1000)
    from temporalio.common import WorkflowIDConflictPolicy
    rebuilt, skipped = 0, 0
    t_reb = time.time()
    for tag, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL)):
        for s in ctl.sids(tag, n):
            st = await asyncio.to_thread(reconstruct, s, tag == L, now_ms)
            if st is None:
                skipped += 1
                continue
            await client2.start_workflow("SessionWorkflow", st, id=ctl.wid(s), task_queue="f8",
                                         id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
            rebuilt += 1
    reb_s = round(time.time() - t_reb, 1)
    await asyncio.gather(*tasks)
    out_st = {}
    for tag, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL)):
        out_st[tag] = (await lib.wait_done(client2, tag, n, 900))[0]
    vA, vP, vX = ctl.verify(A, nA, turns), ctl.verify(P, nP, 4), ctl.verify(X, nX, 4)
    tmP = timer_check(P, nP, R, nR, roll, approvedP)
    tmX = timer_check(X, nX, R, nR, roll, approvedX)
    tmL = timer_check(L, nL, R, nR, roll, [])
    finished = {t: fin(t, n) for t, n in ((A, nA), (P, nP), (R, nR), (X, nX), (L, nL))}
    lib.stop_worker("f8w2")
    lib.stop_server()
    out = {"id": "F8", "engine_discarded_at_s": round(cut - t0, 1), "sessions": nA + nP + nR + nX + nL, "rebuilt": rebuilt,
           "already_finished_before_cut": skipped, "rebuild_wall_s": reb_s,
           "new_engine": "fresh server 1.32.2 (CLI 1.10.1), empty SQLite, new worker identity; old SQLite file deleted",
           "doorbells_dropped_by_new_engine_not_found": stats.get("doorbell_dropped", 0), "statuses": out_st,
           "finished_in_postgres": finished, "grant_results_P": {g: gP.count(g) for g in set(gP)},
           "granted_with_lost_doorbell": {g: lost_results.count(g) for g in set(lost_results)}, "verify_A": vA, "verify_P": vP, "timers_main_groups": tmP,
           "stored_in_postgres_and_used": ["session_started.params", "turn_input events (unprocessed = no result row)",
                                          "turn_result / rollup events (rollup deadline = last rollup deadline + interval)",
                                          "approval_requested (request id, deadline_ms)", "approval_grant bound to the request id, written BEFORE the signal"],
           "loss_if_not_stored": {"grant_only_as_signal_X": {"verify": vX["PASS"], "outcomes": tmX},
                                  "deadline_not_stored_L": {"outcomes": tmL}},
           "PASS": vA["PASS"] and vP["PASS"] and tmP["approvals_exactly_once_and_correct"] and tmP["rollups_exactly_once"]
           and set(gP) <= {"granted"} and set(lost_results) <= {"granted"}
           and all(finished[t] == n for t, n in ((A, nA), (P, nP), (R, nR)))
           and not any(k in ("RUNNING", "FAILED", "TERMINATED", "TIMED_OUT") for t in (A, P, R) for k in out_st[t])}
    lib.save(f"f8{suffix()}", out)
    print(out["PASS"], rebuilt, skipped, tmP, tmX, tmL)


# ------------------------------------------------------------------ F7 on Postgres: refused upgrades (controls)
async def f7pgctl():
    """Controls for the Postgres upgrade path: (A) a 1.32.2 server started on the 1.31 schema WITHOUT temporal-sql-tool
    update-schema, and (B) a 1.31.5 server started on the upgraded 1.32 schema (a downgrade). Both are recorded as they
    behave; the documented path (schema tool first) is the F7 run itself."""
    assert lib.BACKEND == "pg"
    old, new = arg("--pg-old", "1.30.9", str), arg("--pg-new", "1.31.5", str)
    lib.set_pg_versions(old, new)
    lib.record(server_old=old, server_new=new)
    db = f"f7pgctl{suffix()}-{int(time.time())}.db"
    lib.start_server("1.7.3", db, TPORT)
    dbname = lib.TPG_DBS[-1]
    cname = f"emlab-temporal-16096-{TPORT}"
    v0 = lib.tpg_schema_versions(dbname)
    lib.stop_server()
    lib.tpg_snapshot(dbname, "pre")         # the rollback path: restore this snapshot, never redeploy the old image

    def attempt(version, label):
        return attempt_on(version, dbname, label)

    def attempt_on(version, dbn, label):
        lib.tpg_start(version, dbn, TPORT, cname)
        t0 = time.time()
        up = False
        while time.time() - t0 < 60:
            rc, st = lib._dk("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cname)
            if rc == 0 and st.startswith("false"):
                break
            if lib.port_open(TPORT):
                time.sleep(8)      # a server that opens its port may still fail its schema check; look again
                rc, st = lib._dk("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cname)
                up = st.startswith("true")
                break
            time.sleep(1)
        rc, st = lib._dk("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cname)
        logs = lib.tpg_logs(cname, 60)
        lib._dk("stop", "-t", "0", cname)
        return {"attempt": label, "server": version, "running_after_wait": st, "port_opened_and_stayed_up": up,
                "log_tail": logs[-700:]}

    a = attempt(new, f"A: {new} on the {old} schema, temporal-sql-tool update-schema skipped")
    lib.start_server("1.10.1", db, TPORT)        # the documented path: schema tool of 1.32.2 first, then the server
    v1 = lib.tpg_schema_versions(dbname)
    lib.stop_server()
    b = attempt(old, f"B: {old} on the upgraded {new} schema (downgrade)")
    # C3: the deploy guard refuses the older image on the upgraded database; today's behaviour (no guard) is attempt B
    try:
        lib.tpg_guard(old, dbname)
        guard_result = {"refused": False}
    except lib.SchemaTooNew as ex:
        guard_result = {"refused": True, "message": str(ex)}
    lib.tpg_restore(dbname, "pre", dbname + "rb")
    rb = attempt_on(old, dbname + "rb", f"C: {old} on the restored pre-upgrade snapshot (the documented rollback)")
    out = {"id": "F7-postgres-upgrade-controls", "deploy_guard_on_downgrade": guard_result, "rollback_by_snapshot_restore": rb, "schema_versions_initial": v0, "schema_versions_after_documented_upgrade": v1,
           "control_A_skipped_schema_update": a, "control_B_downgrade": b,
           "control_A_refused": not a["port_opened_and_stayed_up"], "control_B_refused": not b["port_opened_and_stayed_up"]}
    lib.save(f"f7pgctl{suffix()}", out)
    print("A refused:", out["control_A_refused"], "B refused:", out["control_B_refused"], v0, v1)


if __name__ == "__main__":
    asyncio.run({"f7": f7, "f7ctl": f7ctl, "f8": f8, "f7pgctl": f7pgctl}[sys.argv[1]]())
