"""Seed sets. DEV seeds are strings ("dev-<exp>-<n>") used for development runs only; STAMPED seeds are fixed in THRESHOLDS.md
amendment 3 and used once, on a clean committed tree (POL_SEEDS=stamped). A stamped selection on a dirty tree refuses to run."""
import os
import sys

DEV = {name: ["dev-%s-%d" % (name, i) for i in range(1, 6)] for name in ("e1", "e2", "e3", "e4", "e5", "e6", "b1", "b2", "b3")}
STAMPED = {"e1": [31, 32, 33, 34, 35], "e2": [31, 32, 33, 34, 35], "e3": [99], "e4": [31, 32, 33, 34, 35], "e5": [31, 32, 33, 34, 35],
           "e6": [808], "b1": [31], "b2": [31, 32, 33, 34, 35], "b3": [31]}


def seeds(name):
    mode = os.environ.get("POL_SEEDS", "dev")
    if mode == "stamped":
        if os.environ.get("ALLOW_DIRTY") == "1":
            sys.exit("stamped seeds are never used on a dirty tree")
        return list(STAMPED[name])
    if mode != "dev":
        sys.exit("POL_SEEDS must be dev or stamped")
    return list(DEV[name])
