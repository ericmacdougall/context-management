"""Seed sets. DEV seeds are strings ("dev-<exp>-<n>") used for development runs only; STAMPED seeds are fixed in THRESHOLDS.md
(amendment 5 for this round: fresh integers, never used before) and used once, on a clean committed tree (POL_SEEDS=stamped). A stamped
selection on a dirty tree refuses to run.
Used before and never reused: 11..15, 21..25, 77, 88, 606, 707 (earlier owners), 31..35, 99, 808 (stamped run at 8c03f02), 100..159
(an in-memory probe at f23f119)."""
import os
import sys

DEV = {name: ["dev-%s-%d" % (name, i) for i in range(1, 6)] for name in ("e1", "e2", "e3", "e4", "e5", "e6", "b1", "b2", "b3", "pgdiff", "mig", "s2")}
STAMPED = {"e1": [41, 42, 43, 44, 45], "e2": [41, 42, 43, 44, 45], "e3": [199], "e4": [41, 42, 43, 44, 45], "e5": [41, 42, 43, 44, 45],
           "e6": [909], "b1": [41], "b2": [41, 42, 43, 44, 45], "b3": [41], "pgdiff": [41, 42, 43, 44, 45], "mig": [41, 42, 43]}


def seeds(name):
    mode = os.environ.get("POL_SEEDS", "dev")
    if mode == "stamped":
        if os.environ.get("ALLOW_DIRTY") == "1":
            sys.exit("stamped seeds are never used on a dirty tree")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import stamp            # refuses a dirty git tree; on a synced host reads GIT_STAMP.json
        if stamp().get("dirty") is not False:
            sys.exit("stamped seeds need a clean committed tree (GIT_STAMP dirty=false)")
        for k in ("E2_PER_SEED", "E5_PER_SEED", "B2_PER_SEED", "B1_QUICK", "PGDIFF_LOGS", "PGDIFF_SCEN", "B3_ROUNDS", "B3_DIFF_LOGS"):
            if os.environ.get(k):
                sys.exit("stamped run with dev override %s set" % k)
        return list(STAMPED[name])
    if mode != "dev":
        sys.exit("POL_SEEDS must be dev or stamped")
    return list(DEV[name])
