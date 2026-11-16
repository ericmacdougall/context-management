"""C2: the deadline-edge race between grant_approval and resolve_approval, with real concurrent transactions.
Run from a code tree:  python -m engine.race <label> [iterations]
For each iteration a request with a ~120 ms timeout is created; a granting thread and a resolving thread are released
at random offsets of -4 to +4 ms around the Postgres deadline. Violation = the grant call answered 'granted' but the
resolution is a timeout. Overlap = the grant call's time window overlapped a resolve call's window."""
import json
import pathlib
import platform
import random
import sys
import threading
import time

import engine
from engine import pgstore

T = "t1"


def one(i, results):
    sid = f"race{int(time.time() * 1000) % 10**9}x{i}"
    rid, deadline_ms = pgstore.request_approval(T, sid, "a1", 120)
    gj, rj = random.uniform(-4, 4), random.uniform(-4, 4)
    rec = {"g": None, "g_win": None, "r_wins": [], "resolution": None}

    def grant():
        wait = (deadline_ms + gj) / 1000 - time.time()
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        rec["g"] = pgstore.grant_approval(T, sid, rid, "approver-1")
        rec["g_win"] = (t0, time.time())

    def resolve():
        wait = (deadline_ms + rj) / 1000 - time.time()
        if wait > 0:
            time.sleep(wait)
        while True:
            t0 = time.time()
            r = pgstore.resolve_approval(T, sid, rid)
            rec["r_wins"].append((t0, time.time()))
            if r != "pending":
                rec["resolution"] = r
                return
            time.sleep(0.001)

    th = [threading.Thread(target=grant), threading.Thread(target=resolve)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    results.append(rec)


def main():
    label = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    results, started = [], time.time()
    for i in range(n):
        one(i, results)
    viol = [r for r in results if r["g"] == "granted" and r["resolution"] == "approval_timeout"]
    bad2 = [r for r in results if r["resolution"] == "approval_granted" and r["g"] != "granted"]
    overlap = sum(1 for r in results if r["g_win"] and any(a < r["g_win"][1] and r["g_win"][0] < b for a, b in r["r_wins"]))
    outcomes = {}
    for r in results:
        k = f'{r["g"]}/{r["resolution"]}'
        outcomes[k] = outcomes.get(k, 0) + 1
    root = pathlib.Path(engine.__file__).parent
    gc = root / "GIT_COMMIT"
    out = {"label": label, "iterations": n, "violations_granted_but_timed_out": len(viol), "violations_resolution_granted_without_grant": len(bad2),
           "overlapping_calls": overlap, "outcomes": outcomes,
           "code_dir": str(root.parent), "code_commit": json.loads(gc.read_text()) if gc.exists() else None, "host": platform.node(),
           "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    p = pathlib.Path.home() / "emlab-16096/engine/results" / f"c2-race-{label}-{int(started)}.json"
    p.write_text(json.dumps(out, indent=1))
    print(label, "violations", len(viol), "overlaps", overlap, outcomes)


if __name__ == "__main__":
    main()
