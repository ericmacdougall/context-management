"""E: analyst-job verification cannot be skipped. Completion (a) without a verify result, (b) with verify ok=false,
(c) by a stale claimant after a re-claim, (d) over an input whose subject was erased meanwhile must each be REFUSED by
the store; (e) a proper one is accepted. (h) no read method has a parameter that disables the filter.
Erasure 'done' with residue or with a store missing: exp/review_fix.py F4.
Controls, ONE patch per case: (a,b) the done_needs_verify constraint dropped; (c) the claim-epoch checks of jobs_guard
removed (both places that enforce it); (d) the inputs_erased check of jobs_guard removed.
usage: python -m exp.e_verify [R]"""
import inspect
import json
import sys

import psycopg

from exp.common import write_result
from exp.patches import patched, variant
from exp.util import tenant, msg, session
from wp import db, erasure, keys as K, log, reader

CONSTRAINT = ("constraint done_needs_verify check (state <> 'done' or (verify is not null and (verify->>'ok')::boolean is true "
              "and result_enc is not null))")
PATCH = {
    "ab": (["alter table analyst_jobs drop constraint done_needs_verify"],
           ["set role wp_owner",
            "delete from analyst_jobs where state = 'done' and not (verify is not null and coalesce((verify->>'ok')::boolean, false) and result_enc is not null)",
            "alter table analyst_jobs add " + CONSTRAINT, "reset role"]),
    "c": variant("jobs_guard", [
        ("if NEW.claim_epoch <> OLD.claim_epoch and (NEW.claim_epoch <> OLD.claim_epoch + 1 or NEW.state <> 'claimed') then", "if false then"),
        ("if OLD.state <> 'claimed' or NEW.claim_epoch <> OLD.claim_epoch then", "if OLD.state <> 'claimed' then")]),
    "d": variant("jobs_guard", "if exists (select 1 from events e join subjects s on s.tenant_id = e.tenant_id and s.subject_id = e.subject_id\n"
                               "               where e.tenant_id = NEW.tenant_id and e.event_id = any(NEW.input_event_ids) and s.erased) then",
                 "if false then"),
}
CASES = {"a_no_verify": "ab", "b_verify_false": "ab", "c_stale_claimant": "c", "d_input_erased": "d"}


def attempt(conn, t, jid, ce, verify, enc=b"x"):
    """A NAIVE completion (presents its own claim epoch; no predicate of its own): only the store can refuse it."""
    try:
        with conn.transaction():
            n = conn.execute("update analyst_jobs set state='done', claim_epoch=%s, result_enc=%s, verify=%s where tenant_id=%s and job_id=%s",
                             (ce, enc, json.dumps(verify) if verify is not None else None, t, jid)).rowcount
            return f"accepted:{n}"
    except psycopg.Error as e:
        return f"refused:{getattr(e, 'sqlstate', '?')}"


def one():
    conn, ks = db.connect(), K.KeyService()
    t = tenant("ev")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"m{i}", "V" if i == 3 else "U") for i in range(5)])
    jobs = {j[2][0]: j for j in (log.job_claim(conn, t) for _ in range(5))}
    order = [r[0] for r in conn.execute("select event_id from events where tenant_id=%s order by seq", (t,)).fetchall()]
    jobs = [jobs[e] for e in order]
    r = {}
    r["a_no_verify"] = attempt(conn, t, jobs[0][0], jobs[0][1], None)
    r["b_verify_false"] = attempt(conn, t, jobs[1][0], jobs[1][1], {"ok": False})
    conn.execute("update analyst_jobs set claim_expires = clock_timestamp() - interval '1 s' where tenant_id=%s and job_id=%s", (t, jobs[2][0]))
    reclaim = log.job_claim(conn, t)
    r["c_reclaimed_same_job"] = reclaim[0] == jobs[2][0]
    r["c_stale_claimant"] = attempt(conn, t, jobs[2][0], jobs[2][1], {"ok": True})
    conn.execute("select wp_erasure_mark(%s)", (erasure.request(conn, t, "V"),))     # V's event is job 3's input
    r["d_input_erased"] = attempt(conn, t, jobs[3][0], jobs[3][1], {"ok": True})
    r["e_proper"] = attempt(conn, t, jobs[4][0], jobs[4][1], {"ok": True})
    return r


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sig = {n: list(inspect.signature(getattr(reader.Reader, n)).parameters) for n in ("search", "facts")}
    h_ok = not any(p for ps in sig.values() for p in ps if any(w in p for w in ("filter", "raw", "unsafe", "include", "tomb")))
    out = {"h_reader_signatures": sig, "h_no_filter_switch": h_ok, "patches": {k: v[0] for k, v in PATCH.items()}}
    fen = [one() for _ in range(R)]
    ctl = {}
    for key, (ap, rv) in PATCH.items():
        with patched(ap, rv):
            ctl[key] = [one() for _ in range(R)]
    res = {}
    for case, key in CASES.items():
        f_ref = sum(x[case].startswith("refused") for x in fen)
        c_acc = sum(x[case].startswith("accepted") and x[case] != "accepted:0" for x in ctl[key])
        res[case] = {"fenced_refused": f_ref, "control_accepted": c_acc, "patch": key, "sample": [fen[0][case], ctl[key][0][case]]}
    e_ok = sum(x["e_proper"] == "accepted:1" for x in fen)
    out.update(cases=res, e_proper_accepted=e_ok, fenced_runs=fen, control_runs=ctl)
    out["pass"] = h_ok and e_ok == R and all(v["fenced_refused"] == R and v["control_accepted"] >= int(0.9 * R) for v in res.values())
    print("e_verify", "PASS" if out["pass"] else "FAIL", json.dumps(res), "e_proper", e_ok)
    write_result("e_verify", out)
