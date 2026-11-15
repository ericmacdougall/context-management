"""E: verification cannot be skipped. Analyst job completion (a) without a verify result, (b) with verify ok=false,
(c) by a stale claimant after re-claim, (d) over an input erased meanwhile must each be REFUSED; (e) a proper one is
accepted. Erasure 'done' written directly with residue (f) or with a store missing (g) must be refused.
Reader: (h) no read method accepts a parameter that disables the filter.
Control: the same calls against wp_ctl (no guard triggers / constraint) must accept (a)-(d), (f), (g).
usage: python -m exp.e_verify [R]"""
import inspect
import json
import sys

import psycopg

from exp.common import write_result
from exp.util import tenant, msg, session
from wp import db, keys as K, lease as L, log, reader
from wp.setup import CTL_DB


def attempt(conn, t, jid, ce, verify, enc=b"x"):
    try:
        with conn.transaction():
            # NAIVE completion (no claim predicate of its own): only the store can refuse a stale claimant
            return bool(conn.execute("update analyst_jobs set state='done', claim_epoch=%s, result_enc=%s, verify=%s where tenant_id=%s and job_id=%s returning 1",
                                     (ce, enc, json.dumps(verify) if verify is not None else None, t, jid)).fetchone())
    except psycopg.Error as e:
        return f"refused:{getattr(e, 'sqlstate', '?')}"


def one(arm):
    dbname = CTL_DB if arm == "control" else None
    conn, ks = db.connect(db=dbname) if dbname else db.connect(), K.KeyService()
    t = tenant("ev")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"m{i}", "U") for i in range(5)])
    jobs = [log.job_claim(conn, t) for _ in range(5)]
    r = {}
    r["a_no_verify"] = attempt(conn, t, jobs[0][0], jobs[0][1], None)
    r["b_verify_false"] = attempt(conn, t, jobs[1][0], jobs[1][1], {"ok": False})
    conn.execute("update analyst_jobs set claim_expires = clock_timestamp() - interval '1 s' where tenant_id=%s and job_id=%s", (t, jobs[2][0]))
    reclaim = log.job_claim(conn, t)
    r["c_reclaimed_same_job"] = reclaim[0] == jobs[2][0]
    r["c_stale_claimant"] = attempt(conn, t, jobs[2][0], jobs[2][1], {"ok": True})
    eid = jobs[3][2][0]
    conn.execute("insert into tombstones(tenant_id, event_id, subject_id, job_id) values (%s,%s,'U','manual')", (t, eid))
    r["d_input_erased"] = attempt(conn, t, jobs[3][0], jobs[3][1], {"ok": True})
    r["e_proper"] = attempt(conn, t, jobs[4][0], jobs[4][1], {"ok": True})
    conn.execute("insert into erasure_jobs(job_id, tenant_id, subject_id, lease_scope, lease_epoch) values (%s,%s,'U','projector',0)", ("ej" + t, t))
    pl = L.acquire(conn, t, L.PROJECTOR, 30)
    for k, v in (("f_done_with_residue", {"pg:events": 0, "pg:quarantine": 0, "pg:inbox": 0, "pg:analyst_jobs": 0, "pg:facts": 0, "keys:subject_keys": 0, "es:events": 1, "es:facts": 0}),
                 ("g_done_missing_store", {"pg:events": 0})):
        try:
            with conn.transaction():
                conn.execute("update erasure_jobs set state='done', verify=%s, lease_scope='projector', lease_epoch=%s where job_id=%s", (json.dumps(v), pl.epoch, "ej" + t))
            r[k] = True
            conn.execute("update erasure_jobs set state='fenced' where job_id=%s", ("ej" + t,))
        except psycopg.Error as e:
            r[k] = f"refused:{getattr(e, 'sqlstate', '?')}"
    must_refuse = ["a_no_verify", "b_verify_false", "c_stale_claimant", "d_input_erased", "f_done_with_residue", "g_done_missing_store"]
    refused = {k: r[k] is not True for k in must_refuse}
    holds = all(refused.values()) and r["e_proper"] is True
    return holds, ",".join(k for k, v in refused.items() if not v), r


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sig = {n: list(inspect.signature(getattr(reader.Reader, n)).parameters) for n in ("search", "facts")}
    h_ok = not any(p for ps in sig.values() for p in ps if any(w in p for w in ("filter", "raw", "unsafe", "include", "tomb")))
    out = {"h_reader_signatures": sig, "h_no_filter_switch": h_ok}
    for arm in ("fenced", "control"):
        runs = []
        for _ in range(R):
            h, why, w = one(arm)
            runs.append({"holds": h, "why": why, "witness": w})
        out[arm] = {"holds": sum(x["holds"] for x in runs), "violated": sum(not x["holds"] for x in runs), "runs": runs}
        print(arm, out[arm]["holds"], out[arm]["violated"], runs[0])
    out["pass"] = h_ok and out["fenced"]["holds"] == R and out["control"]["violated"] >= int(0.9 * R)
    print("PASS" if out["pass"] else "FAIL")
    write_result("e_verify", out)
