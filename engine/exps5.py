"""F5 (code evolution) and F6 (state migration) experiments: python -m engine.exps5 <f5a|f5b1|f5b2|f5b3|f5c|f6> [--small]"""
import asyncio
import importlib
import json
import subprocess
import sys
import time

from temporalio.api.enums.v1 import EventType
from temporalio.worker import Replayer

from . import ctl, keys, lib, pgstore
from .common import HOME, TENANT
from .exps import SMALL, T, TPORT, sizes, suffix


def codes(tag):
    rows = lib.q("select session_id, mem_dec(tenant_id,subject_id,content_enc)::json->>'code' from events "
                 "where kind='turn_result' and session_id like %s order by session_id, seq", tag + "%")
    d = {}
    for s, c in rows:
        d.setdefault(s, []).append(c)
    return d


def runs_of(seq):
    """Run-length encode a code sequence: ['v1','v1','v2'] -> ['v1','v2']."""
    out = []
    for c in seq:
        if not out or out[-1] != c:
            out.append(c)
    return out


def code_profile(tag):
    prof = {}
    for s, seq in codes(tag).items():
        key = ">".join(runs_of(seq))
        prof[key] = prof.get(key, 0) + 1
    return prof


async def history_facts(client, sid):
    h = await client.get_workflow_handle(ctl.wid(sid)).fetch_history()
    ev = h.events
    wft_failed = [e for e in ev if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED]
    markers = [e for e in ev if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED]
    msg = ""
    if wft_failed:
        a = wft_failed[-1].workflow_task_failed_event_attributes
        msg = (a.failure.message or "")[:160]
    return {"events": len(ev), "wft_failed": len(wft_failed), "markers": len(markers), "last_failure": msg}


async def replay(client, tag, n, variant):
    mod = importlib.import_module(f"engine.wf.{variant}")
    hists = []
    for s in ctl.sids(tag, n):
        hists.append(await client.get_workflow_handle(ctl.wid(s)).fetch_history())

    async def gen():
        for h in hists:
            yield h
    r = await Replayer(workflows=[mod.SessionWorkflow], data_converter=client.data_converter).replay_workflows(
        gen(), raise_on_replay_failure=False)
    fails = r.replay_failures
    sample = next(iter(fails.values()), None)
    return {"histories": len(hists), "replay_failures": len(fails),
            "failure_type": type(sample).__name__ if sample else None, "failure_sample": str(sample)[:240] if sample else None}


async def progress_window(tag, n, seconds):
    before = lib.q("select count(*) from events where kind='turn_result' and session_id like %s", tag + "%")[0][0]
    await asyncio.sleep(seconds)
    after = lib.q("select count(*) from events where kind='turn_result' and session_id like %s", tag + "%")[0][0]
    return before, after


# ------------------------------------------------------------------ F5a incompatible change
async def f5a():
    n, turns, period, pre = sizes((20, 40, 3, 50), (4, 12, 1.5, 12))
    lib.start_server("1.7.3", f"f5a{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f5a1", "venv-new", "v1", "old", "f5a")
    client, _ = await lib.connect_retry()
    tag, stats = T("u"), {"appended": 0, "signaled": 0, "retries": 0}
    tasks = await lib.launch(client, tag, n, "f5a", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(pre)
    lib.stop_worker("f5a1")
    out = {"id": "F5a", "sessions": n, "turns": turns, "sdk": "temporalio 1.36.1", "server": "1.31.2"}
    out["replay_before_deploy"] = {v: await replay(client, tag, n, v) for v in ("v1", "v2p", "v2i")}
    # deploy the incompatible code anyway
    t_deploy = time.time()
    lib.start_worker("f5a2", "venv-new", "v2i", "new", "f5a")
    b, a = await progress_window(tag, n, 30)
    st = await lib.statuses(client, tag, n)
    facts = [await history_facts(client, s) for s in ctl.sids(tag, n)]
    out["deployed_incompatible"] = {
        "results_before_after_30s": [b, a], "statuses": {k: list(st.values()).count(k) for k in set(st.values())},
        "sessions_with_wft_failures": sum(1 for f in facts if f["wft_failed"]), "max_wft_failed_events": max(f["wft_failed"] for f in facts),
        "sample_failure": next((f["last_failure"] for f in facts if f["wft_failed"]), None),
        "workflow_failed_or_terminated": sum(1 for v in st.values() if v not in ("RUNNING",)),
        "driver_inputs_appended_meanwhile": stats["appended"],
        "visibility": "workflow status stays RUNNING; the only signals are WorkflowTaskFailed events in history, pending_workflow_task attempts in describe, and worker logs"}
    lib.stop_worker("f5a2")
    # roll back to the compatible code: the stuck sessions resume
    lib.start_worker("f5a3", "venv-new", "v1", "old2", "f5a")
    await asyncio.gather(*tasks)
    stt, waited = await lib.wait_done(client, tag, n, 600)
    v = ctl.verify(tag, n, turns)
    lib.stop_worker("f5a3")
    lib.stop_server()
    rb = out["replay_before_deploy"]
    out.update(rollback={"statuses": stt, "wait_s": waited}, verify=v, code_profile=code_profile(tag))
    out["PASS"] = (rb["v1"]["replay_failures"] == 0 and rb["v2p"]["replay_failures"] == 0 and rb["v2i"]["replay_failures"] > 0
                   and out["deployed_incompatible"]["sessions_with_wft_failures"] > 0 and stt == {"COMPLETED": n} and v["PASS"])
    lib.save(f"f5a{suffix()}", out)
    print(out["PASS"], rb["v2i"]["replay_failures"], out["deployed_incompatible"]["sessions_with_wft_failures"])


# ------------------------------------------------------------------ F5b(1) patching
async def f5b1():
    n, turns, period, pre, n2 = sizes((30, 24, 4, 40, 10), (5, 10, 1.5, 8, 3))
    lib.start_server("1.7.3", f"f5b1{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f5b1a", "venv-new", "v1", "old", "f5b1")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    old, new = T("v"), T("w")
    tasks = await lib.launch(client, old, n, "f5b1", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(pre)
    lib.stop_worker("f5b1a")
    t_switch = time.time()
    lib.start_worker("f5b1b", "venv-new", "v2p", "new", "f5b1")
    tasks += await lib.launch(client, new, n2, "f5b1", stats, turns=turns // 2, period=period, expected=turns // 2)
    await asyncio.gather(*tasks)
    st, w1 = await lib.wait_done(client, old, n, 600)
    st2, w2 = await lib.wait_done(client, new, n2, 600)
    v1, v2 = ctl.verify(old, n, turns), ctl.verify(new, n2, turns // 2)
    facts = [await history_facts(client, s) for s in ctl.sids(old, n)[:5]]
    lib.stop_worker("f5b1b")
    lib.stop_server()
    po, pn = code_profile(old), code_profile(new)
    out = {"id": "F5b-1-patching", "old_sessions": n, "new_sessions": n2, "turns": turns,
           "old_worker_needed_after_switch_s": 0, "old_code_profile": po, "new_code_profile": pn,
           "statuses_old": st, "statuses_new": st2, "verify_old": v1, "verify_new": v2, "history_sample_markers": facts,
           "PASS": v1["PASS"] and v2["PASS"] and st == {"COMPLETED": n} and st2 == {"COMPLETED": n2}
           and all(k.endswith("v2p+policy") for k in pn) and all(">" in k or k == "v2p+policy" for k in po)
           and not any(k.startswith("v2p+policy>v1") for k in po)}
    lib.save(f"f5b1{suffix()}", out)
    print(out["PASS"], po, pn)


# ------------------------------------------------------------------ F5b(2) Worker Versioning, pinned
def cli(ver, *args):
    r = subprocess.run([str(HOME / "bin" / f"cli-{ver}" / "temporal"), "--address", f"127.0.0.1:{TPORT}", *args],
                       capture_output=True, text=True, timeout=120)
    return {"rc": r.returncode, "out": (r.stdout + r.stderr)[-600:]}


async def f5b2():
    server_cli = "1.10.1" if "--server-new" in sys.argv else "1.7.3"
    n, turns, period, pre, n2 = sizes((30, 24, 4, 40, 10), (5, 10, 1.5, 10, 3))
    lib.start_server(server_cli, f"f5b2{suffix()}-{int(time.time())}.db", TPORT)
    dep = "sessdep"
    w = ["--deployment", dep, "--pinned"]
    lib.start_worker("f5b2a", "venv-new", "v1", "old", "f5b2", extra=w + ["--build-id", "b1"])
    client, _ = await lib.connect_retry()
    ops = {"server": server_cli}
    await asyncio.sleep(4)
    ops["set_current_b1"] = cli(server_cli, "worker", "deployment", "set-current-version", "--deployment-name", dep,
                                "--build-id", "b1", "--yes")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    old, new = T("x"), T("y")
    tasks = await lib.launch(client, old, n, "f5b2", stats, turns=turns, period=period, expected=turns)
    await asyncio.sleep(pre)
    t_switch = time.time()
    lib.start_worker("f5b2b", "venv-new", "v2i", "new", "f5b2", extra=w + ["--build-id", "b2"])  # incompatible code, new build
    await asyncio.sleep(4)
    ops["set_current_b2"] = cli(server_cli, "worker", "deployment", "set-current-version", "--deployment-name", dep,
                               "--build-id", "b2", "--yes")
    tasks += await lib.launch(client, new, n2, "f5b2", stats, turns=turns // 2, period=period, expected=turns // 2)
    # control: stop the old build's workers for a while: pinned sessions must stall (they depend on old code)
    lib.stop_worker("f5b2a")
    before, after = await progress_window(old, n, 20)
    stalled = after - before
    lib.start_worker("f5b2a", "venv-new", "v1", "old2", "f5b2", extra=w + ["--build-id", "b1"])
    await asyncio.gather(*tasks)
    st, w1 = await lib.wait_done(client, old, n, 900)
    t_old_done = time.time() - t_switch
    st2, w2 = await lib.wait_done(client, new, n2, 600)
    ops["describe_b1"] = cli(server_cli, "worker", "deployment", "describe-version", "--deployment-name", dep, "--build-id", "b1")
    v1, v2 = ctl.verify(old, n, turns), ctl.verify(new, n2, turns // 2)
    lib.stop_worker("f5b2a")
    lib.stop_worker("f5b2b")
    lib.stop_server()
    po, pn = code_profile(old), code_profile(new)
    out = {"id": "F5b-2-worker-versioning-pinned", "server": server_cli, "old_sessions": n, "new_sessions": n2, "ops": ops,
           "old_code_profile": po, "new_code_profile": pn, "old_build_needed_until_s_after_cutover": round(t_old_done, 1),
           "control_old_build_stopped_20s_results_progress": stalled, "statuses_old": st, "statuses_new": st2,
           "verify_old": v1, "verify_new": v2,
           "PASS": v1["PASS"] and v2["PASS"] and st == {"COMPLETED": n} and st2 == {"COMPLETED": n2}
           and set(po) == {"v1"} and set(pn) == {"v2i"}}
    lib.save(f"f5b2{'-newserver' if server_cli != '1.7.3' else ''}{suffix()}", out)
    print(out["PASS"], po, pn, stalled, ops["set_current_b1"]["rc"], ops["set_current_b2"]["rc"])


# ------------------------------------------------------------------ F5b(3) continue-as-new as the upgrade point
async def f5b3():
    n1, n2, turns, period, pre, nn = sizes((15, 15, 24, 4, 40, 10), (3, 3, 10, 1.5, 8, 3))
    lib.start_server("1.7.3", f"f5b3{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f5b3a", "venv-new", "v1c", "old", "f5b3q1")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    g1, g2, gn = T("z"), T("l"), T("m")
    tasks = await lib.launch(client, g1, n1, "f5b3q1", stats, turns=turns, period=period, expected=turns)       # rotate by signal
    tasks += await lib.launch(client, g2, n2, "f5b3q1", stats, turns=turns, period=period, expected=turns, can_len=90)  # natural CAN
    await asyncio.sleep(pre)
    t_cut = time.time()
    # the new code runs on a new queue; the target queue is configuration in Postgres, read by the old code through an activity
    lib.start_worker("f5b3b", "venv-new", "v2c", "new", "f5b3q2")
    with lib.pg_connect(autocommit=True) as c:
        c.execute("insert into engine_config(name,value) values ('target_queue','f5b3q2') on conflict (name) do update set value=excluded.value")
    for s in ctl.sids(g1, n1):
        await client.get_workflow_handle(ctl.wid(s)).signal("rotate_now", "r")
    tasks += await lib.launch(client, gn, nn, "f5b3q2", stats, turns=turns // 2, period=period, expected=turns // 2)
    # when has every old session hopped? (task queue of the latest run)
    hop = {}
    t0 = time.time()
    while len(hop) < n1 + n2 and time.time() - t0 < 600:
        for s in ctl.sids(g1, n1) + ctl.sids(g2, n2):
            if s in hop:
                continue
            d = await client.get_workflow_handle(ctl.wid(s)).describe()
            if d.status.name != "RUNNING" or d.task_queue == "f5b3q2":
                hop[s] = round(time.time() - t_cut, 1)
        await asyncio.sleep(2)
    await asyncio.gather(*tasks)
    sa, _ = await lib.wait_done(client, g1, n1, 900)
    sb, _ = await lib.wait_done(client, g2, n2, 900)
    sc, _ = await lib.wait_done(client, gn, nn, 600)
    t_all = time.time() - t_cut
    va, vb, vc = ctl.verify(g1, n1, turns), ctl.verify(g2, n2, turns), ctl.verify(gn, nn, turns // 2)
    res = await lib.results_of(client, g1, n1)
    lib.stop_worker("f5b3a")
    lib.stop_worker("f5b3b")
    lib.stop_server()
    out = {"id": "F5b-3-continue-as-new-hop", "rotate_group": n1, "natural_can_group": n2, "new_sessions": nn,
           "hop_seconds_after_cutover_max_by_group": {"rotate": max(hop[s] for s in ctl.sids(g1, n1)),
                                                       "natural_can": max(hop[s] for s in ctl.sids(g2, n2))},
           "old_worker_needed_until_s_after_cutover": max(hop.values()), "all_done_s_after_cutover": round(t_all, 1),
           "code_profile_rotate": code_profile(g1), "code_profile_natural": code_profile(g2), "code_profile_new": code_profile(gn),
           "statuses": [sa, sb, sc], "verify": [va, vb, vc], "sample_result": next(iter(res.values())),
           "PASS": va["PASS"] and vb["PASS"] and vc["PASS"] and sa == {"COMPLETED": n1} and sb == {"COMPLETED": n2}
           and sc == {"COMPLETED": nn} and set(code_profile(gn)) == {"v2c"}
           and all(k.startswith("v1c") for k in list(code_profile(g1)) + list(code_profile(g2)))}
    lib.save(f"f5b3{suffix()}", out)
    print(out["PASS"], out["code_profile_rotate"], out["code_profile_natural"], out["old_worker_needed_until_s_after_cutover"])


# ------------------------------------------------------------------ F5c idle across two versions
async def f5c():
    n, wake_wait = sizes((10, 20), (3, 6))
    lib.start_server("1.7.3", f"f5c{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f5c1", "venv-new", "v1", "v1", "f5c")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    ok_tag, bad_tag = T("i"), T("j")
    # two turns each, then idle (expected 3)
    for tag in (ok_tag, bad_tag):
        tasks = await lib.launch(client, tag, n, "f5c", stats, turns=2, period=0.5, expected=3)
        await asyncio.gather(*tasks)
    for _ in range(120):
        done = lib.q("select count(*) from events where kind='turn_result' and (session_id like %s or session_id like %s)", ok_tag + "%", bad_tag + "%")[0][0]
        if done >= 4 * n:
            break
        await asyncio.sleep(1)
    lib.stop_worker("f5c1")
    lib.start_worker("f5c2", "venv-new", "v2p", "v2", "f5c")       # version 2 while idle
    await asyncio.sleep(wake_wait)
    lib.stop_worker("f5c2")
    lib.start_worker("f5c3", "venv-new", "v3p", "v3", "f5c")       # version 3 while idle
    await asyncio.sleep(wake_wait)

    async def wake(tag):
        for s in ctl.sids(tag, n):
            eid = await asyncio.to_thread(pgstore.append, s, "turn_input", {"n": 3, "text": f"wake turn of {s}"})
            await client.get_workflow_handle(ctl.wid(s)).signal("turn", eid)
    await wake(ok_tag)
    st_ok, w_ok = await lib.wait_done(client, ok_tag, n, 240)
    v_ok = ctl.verify(ok_tag, n, 3)
    # control: the SAME wake but the code had the patch "cleaned up" (v2i: unguarded step) -> idle pre-patch sessions break
    lib.stop_worker("f5c3")
    lib.start_worker("f5c4", "venv-new", "v2i", "cleaned", "f5c")
    await wake(bad_tag)
    await asyncio.sleep(25)
    st_bad = await lib.statuses(client, bad_tag, n)
    facts = [await history_facts(client, s) for s in ctl.sids(bad_tag, n)]
    lib.stop_worker("f5c4")
    lib.start_worker("f5c5", "venv-new", "v3p", "v3b", "f5c")      # restore the patched code: they recover
    st_bad2, _ = await lib.wait_done(client, bad_tag, n, 240)
    v_bad2 = ctl.verify(bad_tag, n, 3)
    lib.stop_worker("f5c5")
    lib.stop_server()
    out = {"id": "F5c", "sessions_each_group": n, "idle_through": ["v2p", "v3p"], "statuses_woken_on_v3p": st_ok, "verify_v3p": v_ok,
           "code_profile_v3p": code_profile(ok_tag), "control_cleaned_up_patch": {
               "statuses_after_25s": {k: list(st_bad.values()).count(k) for k in set(st_bad.values())},
               "sessions_with_wft_failures": sum(1 for f in facts if f["wft_failed"]), "sample": next((f["last_failure"] for f in facts if f["wft_failed"]), None),
               "after_restoring_patched_code": st_bad2, "verify_after_restore": v_bad2},
           "required": "v3 must still contain the v2 patch (workflow.patched('policy-check')) while any pre-patch execution exists; histories are replayed in full on wake",
           "PASS": st_ok == {"COMPLETED": n} and v_ok["PASS"] and sum(1 for f in facts if f["wft_failed"]) > 0 and v_bad2["PASS"]}
    lib.save(f"f5c{suffix()}", out)
    print(out["PASS"], out["control_cleaned_up_patch"]["sessions_with_wft_failures"])


# ------------------------------------------------------------------ F6 state schema migration
async def f6():
    nA, nB, nC, turns, period, pre = sizes((20, 10, 10, 30, 4, 45), (4, 2, 2, 12, 1.5, 10))
    variant = "v2sx" if "--broken" in sys.argv else "v2s"
    lib.start_server("1.7.3", f"f6{variant}{suffix()}-{int(time.time())}.db", TPORT)
    lib.start_worker("f6a", "venv-new", "v1", "old", "f6")
    client, _ = await lib.connect_retry()
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    A, B, C, D = T("m") + "a", T("m") + "b", T("m") + "c", T("m") + "d"
    # A: already continued-as-new before the upgrade (v1-shaped state in the new run); B: never CAN'd before the upgrade
    tasks = await lib.launch(client, A, nA, "f6", stats, turns=turns, period=period, expected=turns, rollup_s=6, can_len=60)
    tasks += await lib.launch(client, B, nB, "f6", stats, turns=turns, period=period, expected=turns, rollup_s=6)
    await asyncio.sleep(pre)
    # facts about the carried state before the upgrade
    before = {}
    for s in ctl.sids(A, nA)[:2] + ctl.sids(B, nB)[:2]:
        h = await client.get_workflow_handle(ctl.wid(s)).fetch_history()
        first = h.events[0].workflow_execution_started_event_attributes
        st = json.loads(first.input.payloads[0].data)
        before[s] = {"ver": st["ver"], "has_rollups": "rollups" in st, "has_rollup_count": "rollup_count" in st, "cans": st["cans"]}
    lib.stop_worker("f6a")
    lib.start_worker("f6b", "venv-new", variant, "new", "f6")
    tasks += await lib.launch(client, C, nC, "f6", stats, turns=turns // 2, period=period, expected=turns // 2, rollup_s=6, ver=2)
    await asyncio.gather(*tasks)
    res = {}
    for tag, cnt in ((A, nA), (B, nB), (C, nC)):
        st, _ = await lib.wait_done(client, tag, cnt, 600)
        res[tag] = (st, await lib.results_of(client, tag, cnt))
    mism = []
    for tag, cnt in ((A, nA), (B, nB), (C, nC)):
        for s, r in res[tag][1].items():
            pg_roll = len(pgstore.events_of(s, "rollup"))
            pg_turns = len(pgstore.events_of(s, "turn_result"))
            if r.get("ver") != 2 or r.get("rollups") != pg_roll or r.get("turns") != pg_turns:
                mism.append({"sid": s, "result": r, "pg_rollups": pg_roll, "pg_turns": pg_turns})
    # state versions seen across all runs of every session (coexistence)
    versions = {}
    for s in ctl.sids(A, nA)[:3] + ctl.sids(B, nB)[:2] + ctl.sids(C, nC)[:1]:
        vs = []
        async for e in client.list_workflows(f'WorkflowId = "{ctl.wid(s)}"'):
            h = await client.get_workflow_handle(ctl.wid(s), run_id=e.run_id).fetch_history()
            vs.append(json.loads(h.events[0].workflow_execution_started_event_attributes.input.payloads[0].data)["ver"])
        versions[s] = vs
    verA, verB, verC = ctl.verify(A, nA, turns), ctl.verify(B, nB, turns), ctl.verify(C, nC, turns // 2)
    out = {"id": "F6" + ("-control-broken-migration" if variant == "v2sx" else ""), "variant": variant,
           "state_before_upgrade_sample": before, "carried_state_versions_per_run_sample": versions,
           "statuses": {k: v[0] for k, v in res.items()}, "mismatches_vs_postgres": mism[:5], "mismatch_count": len(mism),
           "verify": [verA, verB, verC]}
    if variant == "v2s":
        # rollback test: v2-shaped sessions meet the v1 code again
        tag = T("m") + "e"
        tasks = await lib.launch(client, tag, 4, "f6", stats, turns=10, period=2, expected=10, rollup_s=6, ver=2)
        await asyncio.sleep(8)
        lib.stop_worker("f6b")
        lib.start_worker("f6c", "venv-new", "v1", "rollback", "f6")
        await asyncio.gather(*tasks)
        await asyncio.sleep(30)
        st_rb = await lib.statuses(client, tag, 4)
        facts = [await history_facts(client, s) for s in ctl.sids(tag, 4)]
        out["rollback_to_v1_with_v2_state"] = {
            "statuses": {k: list(st_rb.values()).count(k) for k in set(st_rb.values())},
            "sessions_with_wft_failures": sum(1 for f in facts if f["wft_failed"]), "sample": next((f["last_failure"] for f in facts if f["wft_failed"]), None)}
        lib.stop_worker("f6c")
        lib.start_worker("f6d", "venv-new", "v2s", "forward", "f6")
        st_fw, _ = await lib.wait_done(client, tag, 4, 240)
        out["rollback_to_v1_with_v2_state"]["after_rolling_forward"] = st_fw
        lib.stop_worker("f6d")
    else:
        lib.stop_worker("f6b")
    lib.stop_server()
    if variant == "v2s":
        out["PASS"] = not mism and all(v["PASS"] for v in out["verify"]) and all(s == {"COMPLETED": c} for s, c in
                                                                              ((out["statuses"][A], nA), (out["statuses"][B], nB), (out["statuses"][C], nC)))
    else:
        out["PASS_expected_false"] = not mism
    lib.save(f"f6{'x' if variant == 'v2sx' else ''}{suffix()}", out)
    print(out.get("PASS", out.get("PASS_expected_false")), len(mism), versions)


if __name__ == "__main__":
    asyncio.run({"f5a": f5a, "f5b1": f5b1, "f5b2": f5b2, "f5b3": f5b3, "f5c": f5c, "f6": f6}[sys.argv[1]]())
