"""Delta review D3 (over-erasure through identifiers), identifier rule part (THRESHOLDS amendment 11).

Reproducer from the review: declare "alex" for X; Y writes "Alex from accounting (a different person)"; erase X; Y's
event is destroyed. Per run (R=5), one tenant:
  fenced   declaring "alex" for X is refused (IdentifierRefused); X declares "Alex Kim" instead; after erasing X,
           Y's event still has content and was not tombstoned; X's own event is erased.
  control  (one change: the rule bypassed, identifier_problem always None) "alex" is accepted and Y's event is
           tombstoned with its content nulled by X's erasure.
Also reported (no threshold): the refusal reasons for a fixed list of identifiers.
Not covered here: span redaction (removing only the matched words) is NOT implemented; a mention still removes the
whole event, and a long shared name ("Alex Kim" naming two people) still over-erases. usage: python -m exp.d3_identifier [R]"""
import sys

from exp.common import write_result
from exp.util import tenant, msg, session, env
from wp import erasure, mentions as M


def once(arm):
    from wp import log, projector
    conn, ks, es = env()
    t = tenant("d3")
    s = session(conn, t)
    out = {}
    real = M.identifier_problem
    if arm == "control":
        M.identifier_problem = lambda n: None
    try:
        try:
            M.declare(conn, ks, t, "X", ["alex"])
            out["alex_declared"] = True
        except M.IdentifierRefused as e:
            out["alex_declared"], out["refusal"] = False, str(e)
            M.declare(conn, ks, t, "X", ["Alex Kim"])
    finally:
        M.identifier_problem = real
    log.append(conn, ks, s, [msg(0, "X: my own note", "X"), msg(1, "Alex from accounting (a different person) approved the budget", "Y")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    yid = conn.execute("select event_id from events where tenant_id=%s and subject_id='Y'", (t,)).fetchone()[0]
    rec = erasure.erase(conn, ks, es, t, "X", physical="wait")
    out["x_done"] = rec["done"]
    out["y_content_kept"] = conn.execute("select content_enc is not null from events where tenant_id=%s and event_id=%s", (t, yid)).fetchone()[0]
    out["y_tombstoned"] = conn.execute("select count(*) > 0 from tombstones where tenant_id=%s and event_id=%s", (t, yid)).fetchone()[0]
    return out


def main(r):
    res = {"R": r, "fenced": [once("fenced") for _ in range(r)], "control": [once("control") for _ in range(r)],
           "rule": {x: M.identifier_problem(M.normalize(x)) for x in
                    ["alex", "anna", "the", "the team", "Alex Kim", "yolanda", "Yolanda Park", "XQ-1001", "XQ1001", "Mr Smith"]},
           "assumption": "MIN_SINGLE=6 characters for a single-token identifier without digits, plus a stop-list (wp/mentions.py); safe default, not a user decision"}
    why = []
    if not all((not x["alex_declared"]) and x["x_done"] and x["y_content_kept"] and not x["y_tombstoned"] for x in res["fenced"]):
        why.append("fenced: not 5/5 refused with Y's event kept")
    if not all(x["alex_declared"] and x["y_tombstoned"] and not x["y_content_kept"] for x in res["control"]):
        why.append("control: did not over-erase Y's event 5/5 (reproducer did not reproduce)")
    res["verdict"] = {"pass": not why, "why": why}
    print("d3_identifier", res["verdict"], flush=True)
    write_result("d3_identifier", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
