"""Driver and verifier helpers. The driver plays the API side: a user turn is an event in Postgres first (created
idempotently by a request id), then a doorbell signal carrying only the event id. `verify` checks exactly-once from
Postgres (and the ledger written by the effect), scoped to one tenant and one tag (session ids are `<tag>-<nnnn>`
and tags are matched with an escaped `<tag>-%` pattern, so a tag that is a prefix of another never matches it)."""
import asyncio
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from . import keys, pgstore, state
from .common import DEMO_TENANT, pg_connect

POOL = ThreadPoolExecutor(24)
PG_RETRIES = {"n": 0}      # driver-side Postgres retries (evidence for the Postgres-crash run)


def sids(tag, n):
    return [f"{tag}-{i:04d}" for i in range(1, n + 1)]


def wid(sid, tenant=DEMO_TENANT):
    """Workflow id: tenant and session, so equal session ids of two tenants are two workflows."""
    return f"s:{tenant}:{sid}"


def like(tag):
    esc = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "-%"


async def blocking(fn, *a, budget=180):
    """Run a Postgres call off the event loop. A Postgres restart or dropped connection is retried (every call made
    through here is idempotent: input creation is keyed by a request id, grants by request id, the rest are reads)."""
    import psycopg
    import psycopg_pool
    t0 = time.time()
    while True:
        try:
            return await asyncio.get_running_loop().run_in_executor(POOL, lambda: fn(*a))
        except (psycopg.OperationalError, psycopg.InterfaceError, psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed):
            PG_RETRIES["n"] += 1
            if time.time() - t0 > budget:
                raise
            await asyncio.sleep(1.0)


async def retry(coro_fn, what, stats, budget=600):
    """Retry across server restarts. Returns the call result, 'dropped' when the workflow does not exist (the event
    is in Postgres, reconcile will find it), or None after the budget."""
    t0 = time.time()
    while True:
        try:
            return await coro_fn()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                stats["doorbell_dropped"] = stats.get("doorbell_dropped", 0) + 1
                return "dropped"
            stats["retries"] = stats.get("retries", 0) + 1
            if time.time() - t0 > budget:
                stats.setdefault("failed", []).append(f"{what}: {e.status}")
                return None
            await asyncio.sleep(1.0)
        except Exception as e:  # server restart: the client simply retries
            stats["retries"] = stats.get("retries", 0) + 1
            if time.time() - t0 > budget:
                stats.setdefault("failed", []).append(f"{what}: {type(e).__name__}")
                return None
            await asyncio.sleep(1.0)


async def start_session(client, sid, a, ver, tq, stats, tenant=DEMO_TENANT):
    st = state.new_state(tenant, sid, ver=ver, expected_turns=a.expected, rollup_s=a.rollup_s,
                         approval_at=a.approval_at, approval_to_s=a.approval_to_s, can_len=a.can_len,
                         stop_after_rollups=a.stop_after_rollups)
    for k in ("approval_poll_s", "approval_poll_max_s", "reconcile_s", "reconcile_max_s"):
        if getattr(a, k, None) is not None:
            st[k] = getattr(a, k)
    params = {k: st[k] for k in ("expected_turns", "rollup_s", "approval_at", "approval_to_s", "can_len", "stop_after_rollups")}
    await blocking(pgstore.append_once, tenant, keys.start_key(tenant, sid), sid, "start", "session_started",
                   {"params": params}, "driver", "user_typed")
    return await retry(lambda: client.start_workflow(
        "SessionWorkflow", st, id=wid(sid, tenant), task_queue=tq,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, task_timeout=timedelta(seconds=5)),
        f"start {sid}", stats)


async def feed(client, sid, a, stats, start_delay, tenant=DEMO_TENANT):
    await asyncio.sleep(start_delay)
    for n in range(1, a.turns + 1):
        eid, _ = await blocking(pgstore.append_input, tenant, sid, f"{sid}-turn-{n}",
                                {"n": n, "text": f"synthetic turn {n} of {sid}"})
        stats["appended"] += 1
        async def send():
            await client.get_workflow_handle(wid(sid, tenant)).signal("turn", eid)
            return "ok"
        ok = await retry(send, f"sig {eid}", stats)
        if ok == "dropped":
            stats["dropped"] = stats.get("dropped", 0) + 1
        elif ok == "ok":
            stats["signaled"] += 1
        await asyncio.sleep(a.period * random.uniform(0.7, 1.3))


def latest_request(tenant, sid):
    rows = pgstore.events_of(tenant, sid, "approval_requested")
    return rows[-1][1] if rows else None


async def wait_request(tenant, sid, timeout=120):
    """The approval request is created by the workflow when it reaches the gate; wait for it (a slow host is not a
    reason to record a grant against a request that does not exist yet)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rid = await blocking(latest_request, tenant, sid)
        if rid:
            return rid
        await asyncio.sleep(1)
    return None


async def approve_session(client, sid, tenant=DEMO_TENANT, with_pg=True, approver="approver-1"):
    """The API side of an approval: a grant bound to the session's request id is written to Postgres FIRST, then the
    doorbell. with_pg=False sends only the doorbell (the anti-pattern the F8 control uses)."""
    result = "signal-only"
    if with_pg:
        rid = await wait_request(tenant, sid)
        result = "no-request" if rid is None else await blocking(pgstore.grant_approval, tenant, sid, rid, approver)
    await client.get_workflow_handle(wid(sid, tenant)).signal("approve", "g")
    return result


def verify(tag, n_sessions, turns, tenant=DEMO_TENANT):
    res = {"tag": tag, "tenant": tenant, "sessions": n_sessions, "expect_turns_each": turns, "problems": []}
    ids = sids(tag, n_sessions)
    pat = like(tag)
    prefix = f"{tenant}/"
    with pg_connect() as conn:
        cur = conn.cursor()
        inp = dict(cur.execute("select session_id, count(*) from events where tenant_id=%s and kind='turn_input' and "
                               "session_id like %s escape '\\' group by 1", (tenant, pat)).fetchall())
        resu = cur.execute("select session_id, count(*), count(distinct mem_dec(tenant_id,subject_id,content_enc)::json->>'in') "
                           "from events where tenant_id=%s and kind='turn_result' and session_id like %s escape '\\' group by 1",
                           (tenant, pat)).fetchall()
        res_by = {r[0]: (r[1], r[2]) for r in resu}
        led_by = dict(cur.execute("select ref_event, count(*) from effect_ledger where tenant_id=%s and ref_event like %s "
                                  "escape '\\' group by 1", (tenant, prefix + pat)).fetchall())
        in_ids = {r[0] for r in cur.execute("select event_id from events where tenant_id=%s and kind='turn_input' and "
                                            "session_id like %s escape '\\'", (tenant, pat)).fetchall()}
        eff_by = dict(cur.execute("select result_ref, count(*) from effects where tenant_id=%s and step='effect' and "
                                  "session_id like %s escape '\\' group by 1", (tenant, pat)).fetchall())
        calls = cur.execute("select count(*) from ext_calls where tenant_id=%s and key like %s escape '\\'",
                            (tenant, prefix + "eff/" + prefix + pat)).fetchone()[0]
        res_ev = cur.execute("select count(*) from events where tenant_id=%s and kind='turn_result' and session_id like %s "
                             "escape '\\'", (tenant, pat)).fetchone()[0]
        res_keys = cur.execute("select count(*) from effects where tenant_id=%s and step='turn_result' and session_id like %s "
                               "escape '\\'", (tenant, pat)).fetchone()[0]
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
        ok, bad, cnt = pgstore.verify_session_chain(tenant, s)
        if not ok:
            chain_bad.append((s, bad))
    if chain_bad: res["problems"].append(f"hash chain broken: {chain_bad[:5]}")
    res.update(inputs_total=sum(inp.values()), result_events=res_ev, result_keys=res_keys, ledger_rows=sum(led_by.values()),
               effect_rows=sum(eff_by.values()), external_calls=calls, redundant_external_calls=calls - sum(led_by.values()),
               chains_verified=len(ids) - len(chain_bad))
    run_id = os.environ.get("ENGINE_RUN_ID", "")
    with pg_connect() as conn:
        r = conn.execute("select count(*), coalesce(sum(inspected),0), coalesce(sum(rejected),0), coalesce(max(max_bytes),0) "
                         "from payload_stats where proc like %s", (run_id + "-%",)).fetchone()
    res["payload_check_this_run"] = {"run_id": run_id, "processes_reporting": r[0], "payloads_inspected_at_least": int(r[1]),
                                     "rejected": int(r[2]), "max_payload_bytes": int(r[3])}
    res["PASS"] = not res["problems"]
    return res
