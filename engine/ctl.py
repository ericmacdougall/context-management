"""Driver and verifier. The driver plays the API side: a user turn is an event in Postgres first, then a doorbell
signal carrying only the event id. `verify` checks exactly-once from Postgres (and the ledger written by the effect).

  python -m engine.ctl run --tag a --sessions 200 --turns 50 --period 12 --expected 50 [--rollup-s N] ...
  python -m engine.ctl wait --tag a --sessions 200 --timeout 900
  python -m engine.ctl verify --tag a --sessions 200 --turns 50 --out results/f4a.json
"""
import argparse
import asyncio
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from . import keys, pgstore, state
from .common import TENANT, pg_connect
from .worker import connect

POOL = ThreadPoolExecutor(24)


def sids(tag, n):
    return [f"{tag}{i:04d}" for i in range(1, n + 1)]


def wid(sid):
    return f"sess-{sid}"


async def blocking(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(POOL, lambda: fn(*a))


async def retry(coro_fn, what, stats, budget=600):
    t0 = time.time()
    while True:
        try:
            return await coro_fn()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:  # doorbell for a workflow that does not exist: Postgres still has the event
                stats["doorbell_dropped"] = stats.get("doorbell_dropped", 0) + 1
                return "dropped"
            stats["retries"] = stats.get("retries", 0) + 1
            if time.time() - t0 > budget:
                stats.setdefault("failed", []).append(f"{what}: {e.status}")
                return None
            await asyncio.sleep(1.0)
        except Exception as e:  # server restart, worker-independent: the client simply retries
            stats["retries"] = stats.get("retries", 0) + 1
            if time.time() - t0 > budget:
                stats.setdefault("failed", []).append(f"{what}: {type(e).__name__}")
                return None
            await asyncio.sleep(1.0)


async def start_session(client, sid, a, ver, tq, stats):
    st = state.new_state(TENANT, sid, ver=ver, expected_turns=a.expected, rollup_s=a.rollup_s,
                         approval_at=a.approval_at, approval_to_s=a.approval_to_s, can_len=a.can_len,
                         stop_after_rollups=a.stop_after_rollups)
    await blocking(pgstore.append_once, keys.start_key(sid), sid, "start",
                   "session_started", {"params": {k: st[k] for k in ("expected_turns", "rollup_s", "approval_at",
                                                                      "approval_to_s", "can_len", "stop_after_rollups")}},
                   "driver", "user_typed")
    return await retry(lambda: client.start_workflow(
        "SessionWorkflow", st, id=wid(sid), task_queue=tq, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        task_timeout=timedelta(seconds=5)), f"start {sid}", stats)


async def feed(client, sid, a, stats, start_delay):
    await asyncio.sleep(start_delay)
    for n in range(1, a.turns + 1):
        eid = await blocking(pgstore.append, sid, "turn_input", {"n": n, "text": f"synthetic turn {n} of {sid}"})
        stats["appended"] += 1
        ok = await retry(lambda: client.get_workflow_handle(wid(sid)).signal("turn", eid), f"sig {eid}", stats)
        if ok is not None or True:
            stats["signaled"] += 1
        await asyncio.sleep(a.period * random.uniform(0.7, 1.3))


async def cmd_run(a):
    client, codec = await connect("driver")
    stats = {"appended": 0, "signaled": 0, "retries": 0}
    ids = sids(a.tag, a.sessions)
    t0 = time.time()
    for i in range(0, len(ids), 20):
        await asyncio.gather(*[start_session(client, s, a, a.ver, a.task_queue, stats) for s in ids[i:i + 20]])
    stats["start_s"] = round(time.time() - t0, 1)
    if a.turns:
        await asyncio.gather(*[feed(client, s, a, stats, random.uniform(0, a.period)) for s in ids])
    stats["wall_s"] = round(time.time() - t0, 1)
    codec.flush()
    stats["payload_inspected_driver"] = codec.inspected
    print(json.dumps(stats))
    if a.out:
        json.dump(stats, open(a.out, "w"))


async def cmd_wait(a):
    client, _ = await connect("waiter")
    t0 = time.time()
    left = set(sids(a.tag, a.sessions))
    out = {}
    while left and time.time() - t0 < a.timeout:
        for s in list(left):
            try:
                d = await client.get_workflow_handle(wid(s)).describe()
            except Exception:
                continue
            if d.status != WorkflowExecutionStatus.RUNNING:
                out[s] = d.status.name
                left.discard(s)
        await asyncio.sleep(2)
    from collections import Counter
    print(json.dumps({"statuses": Counter(out.values()), "still_running": len(left), "wait_s": round(time.time() - t0, 1)}))


def verify(tag, n_sessions, turns):
    res = {"tag": tag, "sessions": n_sessions, "expect_turns_each": turns, "problems": []}
    ids = sids(tag, n_sessions)
    with pg_connect() as conn:
        cur = conn.cursor()
        like = tag + "%"
        inp = dict(cur.execute("select session_id, count(*) from events where kind='turn_input' and session_id like %s "
                               "group by 1", (like,)).fetchall())
        resu = cur.execute("select session_id, count(*), count(distinct mem_dec(tenant_id,subject_id,content_enc)::json->>'in') "
                           "from events where kind='turn_result' and session_id like %s group by 1", (like,)).fetchall()
        res_by = {r[0]: (r[1], r[2]) for r in resu}
        led = cur.execute("select ref_event, count(*) from effect_ledger where ref_event like %s group by 1",
                          (f"{TENANT}/{like}",)).fetchall()
        led_by = dict(led)
        in_ids = {r[0] for r in cur.execute("select event_id from events where kind='turn_input' and session_id like %s",
                                            (like,)).fetchall()}
        eff = cur.execute("select result_ref, count(*) from effects where step='effect' and session_id like %s group by 1",
                          (like,)).fetchall()
        eff_by = dict(eff)
        calls = cur.execute("select count(*) from ext_calls where key like %s", (f"eff/{TENANT}/{like}",)).fetchone()[0]
        res_ev = cur.execute("select count(*) from events where kind='turn_result' and session_id like %s", (like,)).fetchone()[0]
        res_keys = cur.execute("select count(*) from effects where step='turn_result' and session_id like %s", (like,)).fetchone()[0]
    for s in ids:
        if inp.get(s, 0) != turns:
            res["problems"].append(f"{s}: inputs {inp.get(s, 0)} != {turns}")
        r = res_by.get(s, (0, 0))
        if r != (turns, turns):
            res["problems"].append(f"{s}: results rows/distinct {r} != {turns}")
    dup_ledger = {k: v for k, v in led_by.items() if v != 1}
    missing_ledger = in_ids - set(led_by)
    extra_ledger = set(led_by) - in_ids
    bad_eff = {k: v for k, v in eff_by.items() if v != 1}
    missing_eff = in_ids - set(eff_by)
    if dup_ledger: res["problems"].append(f"ledger duplicates: {len(dup_ledger)}")
    if missing_ledger: res["problems"].append(f"ledger missing: {len(missing_ledger)}")
    if extra_ledger: res["problems"].append(f"ledger extra: {len(extra_ledger)}")
    if bad_eff: res["problems"].append(f"effects rows per input != 1: {len(bad_eff)}")
    if missing_eff: res["problems"].append(f"effects missing: {len(missing_eff)}")
    chain_bad = []
    for s in ids:
        ok, bad, cnt = pgstore.verify_session_chain(s)
        if not ok:
            chain_bad.append((s, bad))
    if chain_bad: res["problems"].append(f"hash chain broken: {chain_bad[:5]}")
    res.update(inputs_total=sum(inp.values()), result_events=res_ev, result_keys=res_keys, ledger_rows=sum(led_by.values()),
               effect_rows=sum(eff_by.values()), external_calls=calls, redundant_external_calls=calls - sum(led_by.values()),
               chains_verified=len(ids) - len(chain_bad))
    res["PASS"] = not res["problems"]
    return res


def cmd_verify(a):
    res = verify(a.tag, a.sessions, a.turns)
    with pg_connect() as conn:
        r = conn.execute("select count(*), coalesce(sum(inspected),0), coalesce(sum(rejected),0), coalesce(max(max_bytes),0) "
                         "from payload_stats").fetchone()
    res["payload_check"] = {"processes_reporting": r[0], "payloads_inspected_at_least": int(r[1]), "rejected": int(r[2]),
                            "max_payload_bytes": int(r[3])}
    print(json.dumps(res, indent=1, default=str))
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1, default=str)
    return 0 if res["PASS"] else 1


async def cmd_signal(a):
    client, _ = await connect("ctl")
    for s in sids(a.tag, a.sessions):
        if a.what == "approve":
            await blocking(pgstore.append_once, keys.approval_key(s, "grant"), s, "approval", "approval_grant", {}, "approver", "user_typed")
            await client.get_workflow_handle(wid(s)).signal("approve", "g")
        elif a.what == "finish":
            await client.get_workflow_handle(wid(s)).signal("finish_now", "f")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "wait", "verify", "signal"])
    ap.add_argument("--tag", default="a")
    ap.add_argument("--sessions", type=int, default=1)
    ap.add_argument("--turns", type=int, default=0)
    ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--expected", type=int, default=0)
    ap.add_argument("--rollup-s", type=float, default=0)
    ap.add_argument("--approval-at", type=int, default=0)
    ap.add_argument("--approval-to-s", type=float, default=0)
    ap.add_argument("--stop-after-rollups", type=int, default=0)
    ap.add_argument("--can-len", type=int, default=0)
    ap.add_argument("--ver", type=int, default=1)
    ap.add_argument("--task-queue", default="sess")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--what", default="approve")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.cmd == "run":
        asyncio.run(cmd_run(a))
    elif a.cmd == "wait":
        asyncio.run(cmd_wait(a))
    elif a.cmd == "verify":
        sys.exit(cmd_verify(a))
    elif a.cmd == "signal":
        asyncio.run(cmd_signal(a))


if __name__ == "__main__":
    main()
