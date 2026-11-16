"""Integration cases for the review findings that need a live Temporal engine (M1 M2 M4 H3 L1 L2 L3).
Run on t2-dev:   python -m engine.it_cases --code new|old [--cases m1,m2,...]
`--code old` starts the workers from the 62916ae tree (host dir ~/emlab-16096/engine/old): each case must FAIL there
for the stated reason and PASS with `--code new`. The driver always plays the API side with the new client code."""
import asyncio
import json
import subprocess
import sys
import time

from . import ctl, keys, lib, pgstore
from .common import DEMO_TENANT as TENANT
from .common import HOME

CODE = sys.argv[sys.argv.index("--code") + 1] if "--code" in sys.argv else "new"      # new | old (62916ae) | prev (96a80b2)
CODE_DIR = HOME / {"new": "code", "old": "old", "prev": "prev"}[CODE]
RID = lib.RUN_ID.replace("r", "").replace("x", "")[-8:]
TQ = f"it-{CODE}-{RID}"


def tag(base):
    return f"{base}{RID}"


def legacy(snippet):
    """Run python in the OLD code tree (to write old-shaped records the old workflow understands)."""
    r = subprocess.run([str(HOME / "venv-new/bin/python"), "-c", snippet], cwd=str(CODE_DIR), capture_output=True, text=True)
    return r.stdout + r.stderr


async def up(variant, env=None, name="w"):
    lib.start_worker(name, "venv-new", variant, f"it-{CODE}", TQ, cwd=CODE_DIR, env=env)
    await asyncio.sleep(3)


async def wait_for(pred, timeout, step=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return round(time.time() - t0, 1)
        await asyncio.sleep(step)
    return None


def results_of_session(sid):
    return [json.loads(e[3]) for e in pgstore.events_of(TENANT, sid, "turn_result")]


async def signal(client, sid, eid, name="turn"):
    await client.get_workflow_handle(ctl.wid(sid, TENANT)).signal(name, eid)


async def status(client, sid):
    return (await client.get_workflow_handle(ctl.wid(sid, TENANT)).describe()).status.name


async def case_m1(client):
    """A doorbell lost to a LIVE idle session must not hang its input; inputs are processed in sequence order."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("m1")
    await lib.launch(client, t, 6, TQ, stats, turns=0)
    ordt = tag("m1o")
    await lib.launch(client, ordt, 1, TQ, stats, turns=0)
    await asyncio.sleep(3)
    for sid in ctl.sids(t, 6):                                  # input in Postgres, NO signal
        pgstore.append_input(TENANT, sid, "r1", {"n": 1})
    osid = ctl.sids(ordt, 1)[0]
    evs = [pgstore.append_input(TENANT, osid, f"r{i}", {"n": i})[0] for i in (1, 2, 3)]
    await signal(client, osid, evs[2])                          # doorbell only for the LAST input
    waited = await wait_for(lambda: all(len(results_of_session(s)) == 1 for s in ctl.sids(t, 6)) and len(results_of_session(osid)) == 3, 75)
    order = [r["in"] for r in results_of_session(osid)]
    done = {s: len(results_of_session(s)) for s in ctl.sids(t, 6)}
    return {"processed_within_s": waited, "idle_session_results": done, "order_session_results": order,
            "order_expected": evs, "passed": waited is not None and order == evs}


async def case_m2(client):
    """A granted approval resumes within one poll interval even when its doorbell is lost (rebuild/lost signal)."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("m2")
    deadline_s = 120
    tasks = await lib.launch(client, t, 1, TQ, stats, turns=3, period=0.2, expected=3, approval_at=2, approval_to_s=deadline_s)
    sid = ctl.sids(t, 1)[0]
    await asyncio.gather(*tasks)
    ok = await wait_for(lambda: pgstore.events_of(TENANT, sid, "approval_requested"), 60)
    if ok is None:
        return {"passed": False, "error": "no approval request appeared"}
    t_grant = time.time()
    if CODE == "new":
        rid = pgstore.events_of(TENANT, sid, "approval_requested")[0][1]
        granted = pgstore.grant_approval(TENANT, sid, rid, "approver-1")
    else:
        legacy(f"from engine import pgstore, keys; pgstore.append_once(keys.approval_key('{sid}','grant'), '{sid}', 'approval', 'approval_grant', {{}}, tenant='{TENANT}')")
        granted = "legacy-grant"
    # NO doorbell signal: the workflow must notice the grant by itself
    took = await wait_for(lambda: pgstore.events_of(TENANT, sid, "approval_granted"), 40)
    return {"grant_to_resolution_s": took, "grant_result": granted, "approval_deadline_s": deadline_s,
            "passed": took is not None and took <= 30}


async def case_m4(client):
    """An input whose subject was erased must dead-letter and let the session end, not retry forever."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("m4")
    await lib.launch(client, t, 2, TQ, stats, turns=0)
    x, y = ctl.sids(t, 2)
    e1 = pgstore.append_input(TENANT, x, "r1", {"n": 1})[0]
    await signal(client, x, e1)
    await wait_for(lambda: len(results_of_session(x)) == 1, 30)
    e2 = pgstore.append_input(TENANT, x, "r2", {"n": 2})[0]     # unprocessed input...
    from .tests import compat
    compat.erase_subject(TENANT, x)                              # ...then the subject's key is destroyed
    await signal(client, x, e2)
    ey = pgstore.append_input(TENANT, y, "r1", {"n": 1})[0]
    await signal(client, y, ey)
    await wait_for(lambda: len(results_of_session(y)) == 1, 30)
    await signal(client, y, "f", "finish_now")
    t0 = time.time()
    sx = sy = None
    while time.time() - t0 < 45:
        sx, sy = await status(client, x), await status(client, y)
        if sx != "RUNNING" and sy != "RUNNING":
            break
        await asyncio.sleep(2)
    return {"erased_session_status": sx, "control_session_status": sy, "waited_s": round(time.time() - t0, 1),
            "passed": sx == "COMPLETED" and sy == "COMPLETED"}


async def wait_signal_handled(client, sid, timeout=90):
    """Wait until a workflow task has completed AFTER the last signal in the history (the signal was handled)."""
    from temporalio.api.enums.v1 import EventType
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = await client.get_workflow_handle(ctl.wid(sid, TENANT)).fetch_history()
        sig = [e.event_id for e in h.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED]
        done = [e.event_id for e in h.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED]
        if sig and any(d > sig[-1] for d in done):
            return True
        await asyncio.sleep(1)
    return False


async def case_h3(client):
    """A doorbell naming another session's input is rejected before anything is read or marked."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("h3")
    await lib.launch(client, t, 2, TQ, stats, turns=0)
    a, b = ctl.sids(t, 2)
    eb = pgstore.append_input(TENANT, b, "r1", {"n": 1})[0]
    await signal(client, a, eb)                                  # foreign id into A
    await wait_signal_handled(client, a)                         # A's worker has really processed the foreign doorbell
    await asyncio.sleep(6)                                       # (the old code then runs its three activities)
    await signal(client, b, eb)                                  # B's own doorbell
    await wait_for(lambda: len(results_of_session(b)) >= 1, 30)
    await signal(client, a, "f", "finish_now")
    await signal(client, b, "f", "finish_now")
    await wait_for(lambda: False, 3)
    ra, rb = results_of_session(a), results_of_session(b)
    h = await client.get_workflow_handle(ctl.wid(a, TENANT)).fetch_history()
    kinds = [e.event_type for e in h.events]
    from temporalio.api.enums.v1 import EventType
    sig = [e for e in h.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED]
    return {"results_in_A": len(ra), "results_in_B": len(rb), "A_history_events": len(kinds), "A_signals_received": len(sig),
            "A_activities_scheduled": sum(1 for e in h.events if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED),
            "passed": len(ra) == 0 and len(rb) == 1}


async def case_l1(client):
    """A duplicate doorbell after a continue-as-new must not run a processed turn twice (done set carried)."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("l1")
    await lib.launch(client, t, 1, TQ, stats, turns=0, expected=6, can_len=30)
    sid = ctl.sids(t, 1)[0]
    evs = [pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0] for i in (1, 2, 3)]
    for e in evs:
        await signal(client, sid, e)
    await wait_for(lambda: len(results_of_session(sid)) == 3, 40)
    await asyncio.sleep(6)                                       # let at least one continue-as-new happen
    await signal(client, sid, evs[0])                            # duplicate doorbell for input 1
    await asyncio.sleep(2)
    more = [pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0] for i in (4, 5, 6)]
    for e in more:
        await signal(client, sid, e)
    await wait_for(lambda: False, 12)
    d = await client.get_workflow_handle(ctl.wid(sid, TENANT)).describe()
    n = len(results_of_session(sid))
    res = None
    if d.status.name == "COMPLETED":
        res = await client.get_workflow_handle(ctl.wid(sid, TENANT)).result()
    return {"results_in_postgres": n, "workflow_status": d.status.name, "workflow_result": res,
            "passed": n == 6 and d.status.name == "COMPLETED" and res is not None and res["turns"] == 6 and res["cans"] >= 1}


async def case_l2(client):
    """A finish request received before a continue-as-new must survive it."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("l2")
    await lib.launch(client, t, 1, TQ, stats, turns=0, can_len=30)
    sid = ctl.sids(t, 1)[0]
    evs = [pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0] for i in range(1, 6)]
    for e in evs:
        await signal(client, sid, e)
    await signal(client, sid, "f", "finish_now")
    t0 = time.time()
    st = "RUNNING"
    while time.time() - t0 < 45 and st == "RUNNING":
        await asyncio.sleep(2)
        st = await status(client, sid)
    n = len(results_of_session(sid))
    return {"workflow_status": st, "results": n, "passed": st == "COMPLETED" and n == 5}


async def case_l3(client):
    """Signals that arrive while the continue-as-new hop reads its configuration must be counted and carried."""
    variant = "v1c"
    with lib.pg_connect(autocommit=True) as c:        # no queue hop in this case (a left-over value would strand the session)
        c.execute("delete from engine_config where name='target_queue'")
    await up(variant)
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("l3")
    await lib.launch(client, t, 1, TQ, stats, turns=0, can_len=30, expected=11)
    sid = ctl.sids(t, 1)[0]
    lock = lib.pg_connect(autocommit=False)                      # holds engine_config so get_config blocks for the window
    sent = 0
    for i in range(1, 4):
        e = pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0]
        await signal(client, sid, e)
        sent += 1
    lock.execute("lock table engine_config in access exclusive mode")
    for i in range(4, 12):                                       # doorbells during the blocked read
        e = pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0]
        await signal(client, sid, e)
        sent += 1
        await asyncio.sleep(0.5)
    lock.rollback()
    lock.close()
    t0 = time.time()
    st = "RUNNING"
    while time.time() - t0 < 60 and st == "RUNNING":
        await asyncio.sleep(2)
        st = await status(client, sid)
    res = await client.get_workflow_handle(ctl.wid(sid, TENANT)).result() if st == "COMPLETED" else None
    n = len(results_of_session(sid))
    dbg = None
    if res is None:
        h = await client.get_workflow_handle(ctl.wid(sid, TENANT)).fetch_history()
        dbg = {"events": len(h.events), "last": [str(e.event_type) for e in h.events[-6:]]}
    return {"doorbells_sent": sent, "workflow_result": res, "results_in_postgres": n, "history_debug": dbg,
            "passed": res is not None and res["sig"] >= sent and n == sent}


async def case_n2(client):
    """A doorbell for a sequence that does not exist yet must not cause that input to be skipped later."""
    await up("v1")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("n2")
    await lib.launch(client, t, 1, TQ, stats, turns=0)
    sid = ctl.sids(t, 1)[0]
    first = pgstore.append_input(TENANT, sid, "r0", {"n": 0})[0]
    await signal(client, sid, first)
    await wait_for(lambda: len(results_of_session(sid)) == 1, 30)
    seq = int(first.rsplit("/", 1)[1])
    await signal(client, sid, f"{TENANT}/{sid}/{seq + 8}")       # the future id (a lagging replica, or a hostile caller)
    await asyncio.sleep(6)
    evs = [pgstore.append_input(TENANT, sid, f"r{i}", {"n": i})[0] for i in range(1, 7)]
    for e in evs:
        await signal(client, sid, e)
    waited = await wait_for(lambda: len(results_of_session(sid)) == 7, 60)
    return {"results": len(results_of_session(sid)), "processed_within_s": waited, "passed": len(results_of_session(sid)) == 7}


async def case_m4b(client):
    """The dead-letter path failing must not fail the workflow: an alert state is surfaced instead (new tree only:
    the failure is injected with a test knob that the older trees do not have)."""
    if CODE != "new":
        return {"passed": False, "skipped": "knob ENGINE_FAIL_DEAD_LETTER exists only in the new tree; code reading only for older trees"}
    await up("v1", env={"ENGINE_FAIL_DEAD_LETTER": "1"})
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    t = tag("m4b")
    await lib.launch(client, t, 1, TQ, stats, turns=0)
    sid = ctl.sids(t, 1)[0]
    e1 = pgstore.append_input(TENANT, sid, "r1", {"n": 1})[0]
    from .tests import compat
    compat.erase_subject(TENANT, sid)
    await signal(client, sid, e1)
    h = client.get_workflow_handle(ctl.wid(sid, TENANT))
    st = "RUNNING"
    t0 = time.time()
    while time.time() - t0 < 60 and st == "RUNNING":
        await asyncio.sleep(2)
        st = await status(client, sid)
    res = await h.result() if st == "COMPLETED" else None
    return {"workflow_status": st, "workflow_result": res,
            "passed": st == "COMPLETED" and res is not None and res["dead_failed"] == 1 and res["alert"].startswith("dead_letter_failed")}


CASES = {"m1": case_m1, "m2": case_m2, "m4": case_m4, "h3": case_h3, "l1": case_l1, "l2": case_l2, "l3": case_l3,
         "n2": case_n2, "m4b": case_m4b}


async def main():
    want = sys.argv[sys.argv.index("--cases") + 1].split(",") if "--cases" in sys.argv else list(CASES)
    lib.start_server("1.7.3", f"it-{CODE}-{RID}.db", 0)
    client, _ = await lib.connect_retry()
    out = {}
    for name in want:
        globals()["TQ"] = f"it-{CODE}-{RID}-{name}"      # one task queue per case
        t0 = time.time()
        try:
            out[name] = await CASES[name](client)
        except Exception as e:  # a case that errors is a failed case, with the reason recorded
            out[name] = {"passed": False, "error": f"{type(e).__name__}: {e}"}
        out[name]["seconds"] = round(time.time() - t0, 1)
        print(name, out[name]["passed"], flush=True)
        lib.stop_worker("w")
    lib.stop_server()
    gc = CODE_DIR / "engine" / "GIT_COMMIT"
    lib.save(f"it-{CODE}-{RID}", {"code": CODE, "code_dir": str(CODE_DIR), "code_commit": json.loads(gc.read_text()) if gc.exists() else None, "task_queue": TQ, "cases": out,
                                 "all_passed": all(v["passed"] for v in out.values())})


if __name__ == "__main__":
    asyncio.run(main())
