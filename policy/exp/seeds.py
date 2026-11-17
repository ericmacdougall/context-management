"""Seed sets. DEV seeds are strings ("dev-<exp>-<n>") used for development runs only; STAMPED seeds are fixed in THRESHOLDS.md
(the latest amendment: fresh integers, never used before) and used once, on a clean committed tree (POL_SEEDS=stamped). A stamped
selection on a dirty tree refuses to run.
Used before and never reused: 11..15, 21..25, 77, 88, 606, 707 (earlier owners), 31..35, 99, 808 (stamped run at 8c03f02), 100..159
(an in-memory probe at f23f119), 41..45, 199, 909 (stamped run at edb693f, amendment 5), 61..65, 299, 919 (stamped run at d05d731,
amendment 6). Amendment 7: 71..75, 399, 929."""
import os
import sys

DEV = {name: ["dev-%s-%d" % (name, i) for i in range(1, 6)] for name in ("e1", "e2", "e3", "e4", "e5", "e6", "b1", "b2", "b3", "pgdiff", "mig", "s2")}
STAMPED = {"e1": [71, 72, 73, 74, 75], "e2": [71, 72, 73, 74, 75], "e3": [399], "e4": [71, 72, 73, 74, 75], "e5": [71, 72, 73, 74, 75],
           "e6": [929], "b1": [71], "b2": [71, 72, 73, 74, 75], "b3": [71], "pgdiff": [71, 72, 73, 74, 75], "mig": [71, 72, 73],
           "s2": [71, 72, 73]}


def seeds(name):
    mode = os.environ.get("POL_SEEDS", "dev")
    if mode == "stamped":
        if os.environ.get("ALLOW_DIRTY") == "1":
            sys.exit("stamped seeds are never used on a dirty tree")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import stamp            # refuses a dirty git tree; on a synced host reads GIT_STAMP.json
        if stamp().get("dirty") is not False:
            sys.exit("stamped seeds need a clean committed tree (GIT_STAMP dirty=false)")
        for k in ("E2_PER_SEED", "E5_PER_SEED", "B2_PER_SEED", "B1_QUICK", "PGDIFF_LOGS", "PGDIFF_SCEN", "B3_ROUNDS", "B3_DIFF_LOGS", "S2_TRIALS"):
            if os.environ.get(k):
                sys.exit("stamped run with dev override %s set" % k)
        return list(STAMPED[name])
    if mode != "dev":
        sys.exit("POL_SEEDS must be dev or stamped")
    return list(DEV[name])
