"""Item 4 of the 4960f50 round (THRESHOLDS amendment 9): "logically erased, physical purge pending".

Fenced (shipped default physical="followup"), R=5, one 10-event subject per run in its own tenant:
  - the runner returns quickly with receipt_state 'logically_erased_physical_purge_pending', done False, and at that
    moment the tenant's events index still holds soft-deleted documents (docs.deleted > 0: bytes on disk);
  - sweep() (physical="wait") then completes it: done, docs.deleted == 0 after, within the store's deadline.
Control (one patch: wp_erasure_complete ignores es:soft_deleted and keys:physical_pending), R=5: the same default
run reports done while docs.deleted > 0 (done claimed early). usage: python -m exp.phys_purge"""
import time

from exp.common import write_result
from exp.patches import patched, variant
from exp.util import tenant, msg, session, env

R = 5


def _deleted(es, t):
    from wp import families as F
    idx = F.base_index(t, "events")
    es.req("POST", f"/{idx}/_refresh")
    return es.req("GET", f"/{idx}/_stats/docs")[1]["_all"]["primaries"]["docs"]["deleted"]


def one(arm):
    from wp import erasure, log, projector
    conn, ks, es = env()
    t = tenant("pp")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"X note {i}", "X") for i in range(10)] + [msg(10, "Y note", "Y")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    t0 = time.time()
    rec = erasure.erase(conn, ks, es, t, "X")                 # shipped default: physical="followup"
    first = {"seconds": round(time.time() - t0, 3), "done": rec["done"], "receipt_state": rec.get("receipt_state"),
             "error_type": rec["error_type"], "physical_pending": rec.get("physical_pending"),
             "docs_deleted_at_receipt": _deleted(es, t)}
    out = {"first": first}
    if arm == "fenced":
        t1 = time.time()
        sw = erasure.sweep(conn, ks, es, tenant=t, physical="wait")
        r2 = {"done": list(sw.values())[0] is True} if sw else {}
        st = conn.execute("select state, extract(epoch from updated_at - logical_at)::float8, updated_at <= physical_deadline "
                          "from erasure_jobs where tenant_id=%s and subject_id='X'", (t,)).fetchone()
        out["followup"] = {"seconds": round(time.time() - t1, 3), "done": r2.get("done"), "state": st[0],
                           "logical_to_done_s": round(st[1], 3) if st[1] is not None else None, "within_deadline": st[2],
                           "docs_deleted_after": _deleted(es, t)}
    return out


def main():
    ap, rv = variant("wp_erasure_complete",
                     "where jsonb_typeof(val) <> 'number' or (val #>> '{}')::numeric <> 0;\n  if bad is not null then",
                     "where k not in ('es:soft_deleted', 'keys:physical_pending') and (jsonb_typeof(val) <> 'number' or (val #>> '{}')::numeric <> 0);\n  if bad is not null then")
    res = {"R": R, "fenced": [one("fenced") for _ in range(R)]}
    with patched(ap, rv):
        res["control"] = [one("control") for _ in range(R)]
    res["patch"] = "control: wp_erasure_complete ignores es:soft_deleted and keys:physical_pending"
    why = []
    for i, r in enumerate(res["fenced"]):
        f, u = r["first"], r["followup"]
        if f["done"] or f["receipt_state"] != "logically_erased_physical_purge_pending" or f["error_type"]:
            why.append(f"fenced[{i}] first receipt {f}")
        if f["seconds"] > 10:
            why.append(f"fenced[{i}] logical receipt took {f['seconds']} s > 10 s")
        if f["docs_deleted_at_receipt"] <= 0 and not f["physical_pending"]:
            why.append(f"fenced[{i}] nothing physical was pending (test does not discriminate)")
        if not (u["done"] and u["state"] == "done" and u["docs_deleted_after"] == 0 and u["within_deadline"]):
            why.append(f"fenced[{i}] follow-up {u}")
    for i, r in enumerate(res["control"]):
        f = r["first"]
        if not (f["done"] and f["docs_deleted_at_receipt"] > 0):
            why.append(f"control[{i}] did not claim done with bytes present: {f}")
    res["verdict"] = {"pass": not why, "why": why}
    print("phys_purge", res["verdict"], flush=True)
    write_result("phys_purge", res)


if __name__ == "__main__":
    main()
