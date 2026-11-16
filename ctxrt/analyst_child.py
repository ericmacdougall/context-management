"""Stub analyst PROCESS for the process-level stop proof (4c). It misbehaves on purpose: it ignores SIGTERM, keeps trying to spend model calls and to
write to the inbox, and never checks any flag. Safety nets so it can never be left behind: a parent-death watchdog and a hard self-timeout.
Every attempt is appended to an attempts file: `<time_ns> <kind> <landed 0|1>`."""
from __future__ import annotations

import argparse
import os
import signal
import threading
import time

from memcore.inbox import InboxItem, InboxService, RequirementGraph
from ctxrt.rundb import BudgetExhausted, Fenced, NestStore, RunDB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--attempts", required=True)
    ap.add_argument("--mode", default="ignore_term")
    ap.add_argument("--self-timeout", type=float, default=40.0)
    a = ap.parse_args()
    parent = os.getppid()
    if a.mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def watchdog():
        t0 = time.time()
        while True:
            time.sleep(0.2)
            if os.getppid() != parent or time.time() - t0 > a.self_timeout:
                os._exit(3)
    threading.Thread(target=watchdog, daemon=True).start()
    store = NestStore(a.db)
    db = RunDB(store)
    inbox = InboxService(store, RequirementGraph({"task": {"req1"}}), time.time)
    fh = open(a.attempts, "a", buffering=1)
    j = 0
    while True:
        j += 1
        for kind in ("spend", "write"):
            landed = 0
            try:
                if kind == "spend":
                    db.spend(a.key, a.epoch)
                else:
                    it = InboxItem("zp-%s-%d" % (a.key[:6], j), "sess", ["ev"], "quote", "transcript", ["req1"], 0.0, 1e12)
                    landed = int(bool(db.write_gate(a.key, a.epoch, lambda: inbox.enqueue(it))))
                    fh.write("%d %s %d\n" % (time.time_ns(), kind, landed))
                    continue
                landed = 1
            except (Fenced, BudgetExhausted):
                pass
            fh.write("%d %s %d\n" % (time.time_ns(), kind, landed))
        time.sleep(0.01)


if __name__ == "__main__":
    main()
