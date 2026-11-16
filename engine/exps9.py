"""Delta-review experiments: python -m engine.exps9 <n1|n3|n5|n6> [--code new|prev] [...]
n1  long approval wait with a short poll: history growth, continue-as-new, exactly-once approval after a boundary
    and after a worker exit (run on the 96a80b2 tree to reproduce the defect, on the new tree to show it fixed)
n3  idle sessions: history events and activity rate per idle session, reconcile backing off (prev vs new)
n5  a real Postgres crash (container stopped with no grace period) under load, no worker exits in the run
n6  a real zombie worker: a task held past its timeout is re-dispatched and completed elsewhere, then the paused worker
    resumes and its late completion is rejected; control: attempt-derived effect keys make the same run duplicate"""
import asyncio
import json
import os
import subprocess
import sys
import time

from . import ctl, lib, pgstore
from .common import DEMO_TENANT as TENANT
from .common import HOME
from .exps import PG_CONTAINER, T, TPORT, arg, count_results, crash_log, exit_summary, sizes, suffix

CODE = sys.argv[sys.argv.index("--code") + 1] if "--code" in sys.argv else "new"
CODE_DIR = HOME / {"new": "code", "prev": "prev"}[CODE]


async def hist(client, sid):
    d = await client.get_workflow_handle(ctl.wid(sid, TENANT)).describe()
    return d.history_length, d.status.name


async def run_count(client, sid):
    n = 0
    async for _ in client.list_workflows(f'WorkflowId = "{ctl.wid(sid, TENANT)}"'):
        n += 1
    return n


# ------------------------------------------------------------------ N1 approval wait history
async def n1():
    to_s, poll, pmax, minutes = arg("--to", 7200), arg("--poll", 0.5), arg("--poll-max", 0.5), arg("--minutes", 15)
    sample_s = 30
    lib.record(code=CODE, approval_timeout_s=to_s, approval_poll_s=poll, approval_poll_max_s=pmax, observe_minutes=minutes)
    lib.start_server("1.7.3", f"n1{CODE}-{int(time.time())}.db", TPORT)
    lib.start_worker("n1w", "venv-new", "v1", "hostA", "n1", cwd=CODE_DIR)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("n1")
    tasks = await lib.launch(client, tag, 1, "n1", stats, turns=1, period=0.1, expected=2, approval_at=1, approval_to_s=to_s,
                             approval_poll_s=poll, approval_poll_max_s=pmax)
    await asyncio.gather(*tasks)
    sid = ctl.sids(tag, 1)[0]
    await ctl.wait_request(TENANT, sid, 120)
    t0 = time.time()
    series, events = [], []
    worker_exit_done = granted = False
    status = "RUNNING"
    while time.time() - t0 < minutes * 60 and status == "RUNNING":
        await asyncio.sleep(sample_s)
        try:
            h, status = await hist(client, sid)
        except Exception as e:
            h, status = None, type(e).__name__
        runs = await run_count(client, sid) if CODE == "new" or len(series) % 4 == 0 else None
        series.append({"t_s": round(time.time() - t0), "history_length": h, "status": status, "runs": runs})
        if CODE == "new":
            # after at least two continue-as-new boundaries: a worker exit DURING the wait, then the grant
            if runs and runs >= 2 and not worker_exit_done:
                rc = lib.stop_worker("n1w", hard=True)
                await asyncio.sleep(3)
                lib.start_worker("n1w", "venv-new", "v1", "hostA", "n1", cwd=CODE_DIR)
                events.append({"t_s": round(time.time() - t0), "what": "worker abrupt exit during the wait", "exit_code": rc, "runs": runs})
                worker_exit_done = True
            if runs and runs >= 3 and worker_exit_done and not granted:
                r = await ctl.approve_session(client, sid, TENANT)
                events.append({"t_s": round(time.time() - t0), "what": "grant after the boundaries and the exit", "result": r, "runs": runs})
                granted = True
                await asyncio.sleep(1)
                eid = pgstore.append_input(TENANT, sid, "after-grant", {"n": 2, "text": "second turn after the approval"})[0]
                await client.get_workflow_handle(ctl.wid(sid, TENANT)).signal("turn", eid)
    final_status = (await hist(client, sid))[1] if series and series[-1]["status"] == "RUNNING" else series[-1]["status"] if series else None
    if CODE == "new" and granted:
        for _ in range(60):
            if (await hist(client, sid))[1] != "RUNNING":
                break
            await asyncio.sleep(2)
        final_status = (await hist(client, sid))[1]
    grants = lib.events_of(sid, "approval_granted")
    results = len(lib.events_of(sid, "turn_result"))
    lib.stop_worker("n1w")
    lib.stop_server()
    hs = [x["history_length"] for x in series if x["history_length"]]
    peak = max(hs) if hs else None
    rate = None
    if len(series) >= 3 and series[0]["history_length"] and series[-1]["history_length"]:
        span = series[-1]["t_s"] - series[0]["t_s"]
        rate = round((series[-1]["history_length"] - series[0]["history_length"]) / span, 2) if span else None
    out = {"id": "N1-approval-wait-" + CODE, "code_tree": str(CODE_DIR), "series": series, "events": events,
           "peak_history_length_observed": peak, "growth_events_per_s_first_to_last_sample": rate,
           "hours_to_hard_limit_at_that_rate": round((51200 - (series[0]["history_length"] or 0)) / rate / 3600, 2) if rate and rate > 0 else None,
           "approval_granted_events": len(grants), "turn_results": results, "final_status": final_status, "doc_limits": {"warn_events": 10240, "hard_events": 51200}}
    if CODE == "prev":
        out["defect_reproduced"] = bool(peak and peak > 10240) or final_status in ("TERMINATED", "FAILED")
    else:
        out["PASS"] = (peak is not None and peak < 10240 and any(x["runs"] and x["runs"] >= 3 for x in series) and granted
                       and len(grants) == 1 and results == 2 and final_status == "COMPLETED" and worker_exit_done)
    lib.save(f"n1-{CODE}{suffix()}", out)
    print(out.get("PASS", out.get("defect_reproduced")), peak, rate, out["approval_granted_events"], final_status)


# ------------------------------------------------------------------ N3 idle cost
def cpu_ticks(pid):
    parts = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])


def pg_xacts():
    return lib.q("select xact_commit + xact_rollback from pg_stat_database where datname='emlab'")[0][0]


async def n3():
    n, minutes = int(arg("--sessions", 200)), arg("--minutes", 10)
    lib.record(code=CODE, idle_sessions=n, observe_minutes=minutes, reconcile_s=20, reconcile_max_s=600)
    lib.start_server("1.7.3", f"n3{CODE}-{int(time.time())}.db", TPORT)
    w = lib.start_worker("n3w", "venv-new", "v1", "hostA", "n3", cwd=CODE_DIR)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("n3")
    await lib.launch(client, tag, n, "n3", stats, turns=0)
    sids = ctl.sids(tag, n)
    sample = sids[:: max(1, n // 20)]
    await asyncio.sleep(60)          # let the first reconciles settle
    marks = []

    async def mark():
        hs = [(await hist(client, s))[0] for s in sample]
        marks.append({"t_s": round(time.time() - t0), "mean_history_events": round(sum(hs) / len(hs), 1), "cpu_ticks": cpu_ticks(w.pid),
                      "pg_xacts": pg_xacts()})
    t0 = time.time()
    await mark()
    for frac in (0.5, 1.0):
        await asyncio.sleep(max(0, minutes * 60 * frac - (time.time() - t0)))
        await mark()
    lib.stop_worker("n3w")
    lib.stop_server()
    per = []
    for a, b in zip(marks, marks[1:]):
        dt = b["t_s"] - a["t_s"]
        per.append({"window_s": [a["t_s"], b["t_s"]], "events_per_session_per_hour": round((b["mean_history_events"] - a["mean_history_events"]) / dt * 3600, 1),
                    "events_per_session_per_day": round((b["mean_history_events"] - a["mean_history_events"]) / dt * 86400),
                    "worker_cpu_s_per_1000_sessions_per_hour": round((b["cpu_ticks"] - a["cpu_ticks"]) / os.sysconf("SC_CLK_TCK") / dt * 3600 / n * 1000, 1),
                    "postgres_transactions_per_s_all_clients": round((b["pg_xacts"] - a["pg_xacts"]) / dt, 1)})
    out = {"id": "N3-idle-cost-" + CODE, "sessions": n, "marks": marks, "windows": per,
           "note": "a session that is idle for a day still replays a growing history; events per day set the continue-as-new cadence"}
    lib.save(f"n3-{CODE}{suffix()}", out)
    print(json.dumps(per))


# ------------------------------------------------------------------ N5 Postgres crash
def docker(*args):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def pg_activity():
    rows = lib.q("select count(*) filter (where state='active'), count(*) filter (where state like 'idle in transaction%'), count(*), "
                 "pg_postmaster_start_time() from pg_stat_activity where datname='emlab'")[0]
    return {"active": rows[0], "idle_in_transaction": rows[1], "clients": rows[2], "postmaster_start": str(rows[3])}


async def n5():
    n, turns, period = sizes((40, 20, 6), (4, 6, 1.5))
    kills = sizes((25, 60), (8, 18))
    errlog = HOME / "logs" / f"n5-{lib.RUN_ID}.errors"
    errlog.write_text("")
    lib.record(sessions=n, turns_each=turns, period_s=period, pg_crash_at_s=list(kills), worker_exit_knobs="none", effect_gap_s=0.05)
    lib.start_server("1.7.3", f"n5-{int(time.time())}.db", TPORT)
    lib.start_worker("n5w", "venv-new", "v1", "hostA", "n5", env={"ENGINE_EFFECT_GAP": "0.05", "ENGINE_ERR_LOG": str(errlog)})
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("n5")
    t0 = time.time()
    tasks = await lib.launch(client, tag, n, "n5", stats, turns=turns, period=period, expected=turns)
    crashes = []
    for at_s in kills:
        await asyncio.sleep(max(0, at_s - (time.time() - t0)))
        before = await asyncio.to_thread(pg_activity)
        t1 = time.time()
        rc, out = await asyncio.to_thread(docker, "stop", "-t", "0", PG_CONTAINER)     # no grace period: the postmaster gets SIGKILL
        _, st = await asyncio.to_thread(docker, "inspect", "--format", "{{.State.ExitCode}} oom={{.State.OOMKilled}}", PG_CONTAINER)
        down_s = 4
        await asyncio.sleep(down_s)
        rc2, _ = await asyncio.to_thread(docker, "start", PG_CONTAINER)
        after = None
        for _ in range(60):
            try:
                after = await asyncio.to_thread(pg_activity)
                break
            except Exception:
                await asyncio.sleep(1)
        crashes.append({"at_s": round(t1 - t0, 1), "stop_rc": rc, "exit_code_and_oom": st, "start_rc": rc2, "before": before, "after": after,
                        "postmaster_restarted": bool(after and after["postmaster_start"] != before["postmaster_start"])})
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 900)
    v = ctl.verify(tag, n, turns)
    errs = [json.loads(l) for l in errlog.read_text().splitlines() if l.strip()]
    by = {}
    for e in errs:
        by[e["class"]] = by.get(e["class"], 0) + 1
    lib.stop_worker("n5w")
    lib.stop_server()
    out = {"id": "N5-postgres-crash", "crashes": crashes, "activity_transient_errors_by_class": by, "activity_transient_errors": len(errs),
           "driver_postgres_retries": ctl.PG_RETRIES["n"], "statuses": st, "verify": v,
           "PASS": v["PASS"] and st == {"COMPLETED": n} and all(c["postmaster_restarted"] for c in crashes)
           and len(errs) > 0 and all(c["before"]["active"] + c["before"]["idle_in_transaction"] > 0 for c in crashes)}
    lib.save(f"n5{suffix()}", out)
    print(out["PASS"], by, ctl.PG_RETRIES, [c["before"] for c in crashes])


# ------------------------------------------------------------------ N6 real zombie
async def n6():
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "ok"      # ok | brokenkeys
    n, turns, period, hold = sizes((10, 6, 3, 28), (3, 4, 1.5, 20))
    pause = HOME / "run" / f"pause-{lib.RUN_ID}"
    pause.write_text("hold")
    lib.record(sessions=n, turns_each=turns, period_s=period, hold_s=hold, start_to_close_s=8, mode=mode)
    lib.start_server("1.7.3", f"n6{mode}-{int(time.time())}.db", TPORT)
    envA = {"ENGINE_PAUSE_FILE": str(pause), "ENGINE_EFFECT_GAP": "0.05"}
    envB = {"ENGINE_EFFECT_GAP": "0.05"}
    if mode == "brokenkeys":
        envA["ENGINE_BROKEN_KEYS"] = envB["ENGINE_BROKEN_KEYS"] = "1"
    lib.start_worker("n6A", "venv-new", "v1", "workerA", "n6", env=envA)
    lib.start_worker("n6B", "venv-new", "v1", "workerB", "n6", env=envB)
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    tag = T("n6" + ("b" if mode == "brokenkeys" else "o"))
    tasks = await lib.launch(client, tag, n, "n6", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(hold)
    resume_t = time.time()
    pause.unlink()                                  # worker A resumes its held tasks, long after they timed out
    await asyncio.sleep(2)
    await asyncio.gather(*tasks)
    st, waited = await lib.wait_done(client, tag, n, 600)
    v = ctl.verify(tag, n, turns)
    rowsA = crash_log("n6A")
    # evidence: A's held tasks timed out and were completed by B; A's late completion was rejected by the server
    from temporalio.api.enums.v1 import EventType
    timed_out = completed_by = 0
    done_by = {}
    for s in ctl.sids(tag, n):
        h = await client.get_workflow_handle(ctl.wid(s, TENANT)).fetch_history()
        for e in h.events:
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
                timed_out += 1
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                ident = e.activity_task_completed_event_attributes.identity
                done_by[ident] = done_by.get(ident, 0) + 1
    logA = (HOME / "logs" / f"{lib._u('n6A')}.log")
    late = []
    if logA.exists():
        for line in logA.read_text(errors="replace").splitlines():
            low = line.lower()
            if ("not found" in low or "notfound" in low or "already" in low) and ("complet" in low or "activity" in low):
                late.append(line[:220])
    lib.stop_worker("n6A")
    lib.stop_worker("n6B")
    lib.stop_server()
    resumed = [r for r in rowsA if r.get("how") == "resumed-after-pause"]
    out = {"id": "N6-zombie-" + mode, "hold_s": hold, "activities_timed_out_in_history": timed_out,
           "activities_completed_by_identity": done_by, "worker_A_resumes": len(resumed),
           "worker_A_late_completion_rejections_logged": len(late), "worker_A_log_samples": late[:3], "statuses": st, "verify": v,
           "ledger_duplicates_problem": [p for p in v["problems"] if "ledger duplicates" in p]}
    expect_pass = mode == "ok"
    out["verify_PASS_expected"] = expect_pass
    # a task held past its timeout is retried elsewhere: the effect is called again (redundant calls) but applied once
    redispatched = v["redundant_external_calls"] > 0 if expect_pass else v["ledger_rows"] > v["inputs_total"]
    out["task_redispatched_evidence"] = {"redundant_external_calls": v["redundant_external_calls"], "ledger_rows": v["ledger_rows"], "inputs": v["inputs_total"]}
    out["as_expected"] = (v["PASS"] == expect_pass) and len(resumed) > 0 and redispatched and (not expect_pass or len(late) > 0)
    lib.save(f"n6-{mode}{suffix()}", out)
    print(mode, v["PASS"], timed_out, len(resumed), len(late), done_by)


if __name__ == "__main__":
    asyncio.run({"n1": n1, "n3": n3, "n5": n5, "n6": n6}[sys.argv[1]]())
