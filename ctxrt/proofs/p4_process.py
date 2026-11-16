"""P4c process level: a stub analyst PROCESS that ignores SIGTERM and keeps trying to spend and write. Run: python3 -m ctxrt.proofs.p4_process"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import time

from memcore.inbox import InboxService, RequirementGraph
from ctxrt import harness
from ctxrt.proofs.common import check, main
from ctxrt.rundb import NestStore, RunDB
from ctxrt.stopper import stop_pid

RUNS = 30


def one_run(rng, i, ledger):
    d = tempfile.mkdtemp(prefix="ctxrt-p4proc-")
    dbp, att = os.path.join(d, "db.sqlite"), os.path.join(d, "attempts.txt")
    store = NestStore(dbp)
    db = RunDB(store)
    db.open_step("step-A", 10 ** 9)
    inbox = InboxService(store, RequirementGraph({"task": {"req1"}}), time.time)
    inbox.register("sess", "host", "task", ["req1"])
    r = db.launch("sess", "step-A", "proc-%d" % i, 1, "v1", 10 ** 6, "o")
    proc = subprocess.Popen([sys.executable, "-m", "ctxrt.analyst_child", "--db", dbp, "--key", r["key"], "--epoch", str(r["epoch"]), "--attempts", att, "--mode", "ignore_term"],
                            cwd=harness.ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(harness.RESULTS, "p4_process_spawn_ledger.jsonl"), "a") as fh:
        fh.write(json.dumps({"pid": proc.pid, "run": i, "purpose": "p4 process stop proof", "ts": time.time()}) + "\n")
    time.sleep(rng.uniform(0.25, 0.45))
    before_calls = db.calls_for(r["key"])
    with store.transaction():
        db.stop_run(r["key"], "p4 process proof")
        snap_calls = store.db.execute("SELECT COALESCE(MAX(id),0) FROM calls").fetchone()[0]
        snap_items = store.db.execute("SELECT COUNT(*) FROM items WHERE session_id='sess'").fetchone()[0]
    t_stop = time.time_ns()
    time.sleep(0.4)                                  # the zombie keeps trying with its fence revoked
    after_calls = store.db.execute("SELECT COUNT(*) FROM calls WHERE run_key=? AND id>?", (r["key"], snap_calls)).fetchone()[0]
    after_items = store.db.execute("SELECT COUNT(*) FROM items WHERE session_id='sess'").fetchone()[0] - snap_items
    lines = [l.split() for l in open(att)] if os.path.exists(att) else []
    pre = [l for l in lines if int(l[0]) < t_stop]
    post = [l for l in lines if int(l[0]) > t_stop + 5_000_000]
    alive_before_stop = proc.poll() is None
    rc, out, err = stop_pid(proc.pid, "ctxrt p4 process stop proof")
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = None
    gone = proc.poll() is not None
    try:
        os.kill(proc.pid, 0)
        pid_exists = True
    except OSError:
        pid_exists = False
    return {"run": i, "calls_before_stop": before_calls, "pre_landed": sum(int(l[2]) for l in pre), "post_attempts_spend": sum(l[1] == "spend" for l in post),
            "post_attempts_write": sum(l[1] == "write" for l in post), "post_landed_attempt_lines": sum(int(l[2]) for l in post), "db_calls_after_stop": after_calls,
            "db_items_after_stop": after_items, "alive_before_stop": alive_before_stop, "ah_rc": rc, "exit_code": code, "gone": gone and not pid_exists, "needed_escalation": code == -9}


def run_seed(seed, params):
    os.makedirs(harness.RESULTS, exist_ok=True)
    rng = random.Random(seed)
    rows = [one_run(rng, i, None) for i in range(RUNS)]
    m = {"runs": len(rows), "gone": sum(r["gone"] for r in rows), "escalated": sum(r["needed_escalation"] for r in rows), "alive_before": sum(r["alive_before_stop"] for r in rows),
         "pre_landed_runs": sum(r["pre_landed"] > 0 for r in rows), "post_spend_attempt_runs": sum(r["post_attempts_spend"] > 0 for r in rows),
         "post_write_attempt_runs": sum(r["post_attempts_write"] > 0 for r in rows), "post_landed_lines": sum(r["post_landed_attempt_lines"] for r in rows),
         "db_calls_after_stop": sum(r["db_calls_after_stop"] for r in rows), "db_items_after_stop": sum(r["db_items_after_stop"] for r in rows),
         "ah_failures": sum(r["ah_rc"] != 0 for r in rows)}
    return rows, m


def evaluate(m, params):
    n = m["runs"]
    return [
        check("process ignoring SIGTERM is gone after the audited stop (100%)", [m["gone"], n], m["gone"] == n == RUNS),
        check("escalation was needed (the stub ignored SIGTERM)", [m["escalated"], n], m["escalated"] == n),
        check("witness: child was alive and landing calls before the fence", [m["alive_before"], m["pre_landed_runs"], n], m["alive_before"] == n and m["pre_landed_runs"] == n),
        check("zombie attempted spend and write after the fence in >=90% of runs", [m["post_spend_attempt_runs"], m["post_write_attempt_runs"], n], m["post_spend_attempt_runs"] >= 0.9 * n and m["post_write_attempt_runs"] >= 0.9 * n),
        check("0 model calls and 0 inbox writes landed after the fence (DB truth and child lines)", [m["db_calls_after_stop"], m["db_items_after_stop"], m["post_landed_lines"]],
              m["db_calls_after_stop"] == 0 and m["db_items_after_stop"] == 0 and m["post_landed_lines"] == 0),
        check("audited stop command succeeded", m["ah_failures"], m["ah_failures"] == 0),
    ]


if __name__ == "__main__":
    main("p4_process", run_seed, evaluate, {"runs": RUNS, "timing": "real time, not quiesced", "stop": "ah.py audited stop with SIGKILL escalation"}, claim="out-of-band stop reaches a process that ignores SIGTERM, its inbox writes and its model calls")
