"""Adds the crash instrumentation summary and run parameters to results/f4a.json (run on the host)."""
import json
import os

H = os.path.expanduser("~/emlab-16096/engine")
rows = [json.loads(l) for l in open(f"{H}/logs/f4a2-worker.crashes")]
inf = [r for r in rows if r["inflight"]]
d = json.load(open(f"{H}/results/f4a.json"))
d["crash_instrumentation"] = {
    "abrupt_worker_exits": len(rows), "with_any_activity_in_flight": len(inf),
    "in_turn_effect_activity": sum(1 for r in rows if r["inflight"].get("turn_effect")),
    "in_turn_append_activity": sum(1 for r in rows if r["inflight"].get("turn_append")),
    "idle_or_between_activities": len(rows) - len(inf),
    "note": "workflow tasks in flight are not counted by this instrumentation"}
d["run"] = {"sessions": 200, "turns_each": 50, "tag": "b", "server": "1.31.2 (CLI 1.7.3)", "sdk": "temporalio 1.31.0",
            "measured_drain_turns_per_s": 13, "injected": "one turn per session per about 30 s, about 6.5 turns/s",
            "effect_gap_ms": 50, "driver_wall_s": 1760.9, "driver_client_retries": 2344,
            "server_stop": "one graceful stop of about 2.5 minutes at 20:47 caused by my own stop-file name collision "
                           "(sessions resumed); the planned abrupt server stop is NOT in this run; see F4c and the targeted runs",
            "drain_after_last_worker_exit_min": 9,
            "finding": "with a worker exit every about 30 s the cache is lost each time and replay of 200 open histories "
                       "outlasts the worker's life, so throughput falls to about 1 turn/s until exits stop; nothing is lost"}
json.dump(d, open(f"{H}/results/f4a.json", "w"), indent=1)
print(d["crash_instrumentation"])
