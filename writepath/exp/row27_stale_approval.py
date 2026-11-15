"""Top-30 row 27 (stale-approval half only): approve at version v, the item is amended to v+1, then the approved action
is executed later. Execution is itself a decision bound to the head version the approval produced, so the store
refuses it once anything changed the item since the approval. A no-amend run must execute (positive control of the
mechanism). Control arm: wp_ctl (no compare-and-set) executes the stale approval.
The parked-session half of row 27 (answers reaching a parked assumption holder) is NOT built.
usage: python -m exp.row27_stale_approval [R]"""
import sys

from exp.common import write_result
from exp.util import tenant, msg, session
from wp import db, keys as K, log
from wp.setup import CTL_DB


def one(arm, amend):
    conn = db.connect(db=CTL_DB) if arm == "control" else db.connect()
    ks = K.KeyService()
    t = tenant("r27")
    s = session(conn, t)
    src = log.append(conn, ks, s, [msg(0, "approve deploy plan v3", "U")])["accepted"][0]
    ts = msg(1, "", "U")["ts_event"]
    v = log.head(conn, ks, t, "s1", "deploy")[0]
    log.decide(conn, ks, s, "U", "deploy", v, "approve", {"plan_version": 3}, src, "ap1", ts)
    if amend:
        log.decide(conn, ks, s, "U", "deploy", v + 1, "amend", {"plan_version": 4}, src, "am1", ts)
    try:
        r = log.decide(conn, ks, s, "U", "deploy", v + 1, "execute", {"approval": "ap1"}, src, "ex1", ts)[0]
    except log.StaleDecision:
        r = "refused"
    return r


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = {a: {"amended": [one(a, True) for _ in range(R)], "not_amended": [one(a, False) for _ in range(R)]} for a in ("fenced", "control")}
    out["pass"] = (out["fenced"]["amended"].count("refused") == R and out["fenced"]["not_amended"].count("accepted") == R
                   and out["control"]["amended"].count("accepted") >= int(0.9 * R))
    print(out)
    write_result("row27_stale_approval", out)
