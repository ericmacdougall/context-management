"""Plan stability of the erasure path (THRESHOLDS amendment 18). Work, not time: for each statement of interest the
rows it processed (sum of actual rows x loops over its plan nodes, plus rows removed by filter, join filter and index
recheck), read from auto_explain on the session that runs the cascade (log_analyze on, log_timing off, nested
statements, plans sent to the client as notices). Planner and plan-cache behaviour are deterministic for a given
statistics state, so the gates do not depend on host load; wall times are recorded beside them as measurements.

Fixture per run: 30 small tenants (20 message events each) and one tenant with subject X of M message events and
subject Y of 5. States: S1 analysed (ANALYZE after every append), S2 stale (ANALYZE after the small tenants only,
autovacuum off on the touched tables for the run, restored afterwards). X is requested and marked by the app role;
then ONE fresh admin session runs wp_erasure_cascade(job, 200) until 0, then wp_erasure_pg_residue(tenant, 'X').
Gates (every run of both states): PS-C work per event of the analyst_jobs and facts deletes in batches 6..10 <= 3x
batches 1..5 and <= 50; PS-R residue work at M=3000 <= 3x at M=1500 (same state, same run index) and no node
removing more than 10 x M rows by join filter. Controls: CONTROLS restores the pre-fix fragments with variant();
each must violate a gate in >= 2 of 3 runs per state. At the pre-fix code the fixed fragments are absent and the
controls are recorded as unavailable; that run is the failing-first evidence.
Amendment 18b: every run starts from a fresh database (wp.setup --reset, then the control's patch); PS-R also bounds
residue work at 100 rows per event; CTL_RESIDUE is required in S1 only (the state where the pre-fix residue misplanned).
usage: python -m exp.plan_stability [R=3] [M1=1500] [M2=3000]"""
import json
import re
import sys
import time

from exp.common import write_result
from exp.patches import function_sql, patched, variant
from exp.util import msg, session, tenant
from wp import db, erasure, log, keys as K, setup as SETUP

BATCH = 200
SMALL_TENANTS, SMALL_EVENTS = 30, 20
TABLES = ("events", "analyst_jobs", "facts", "tombstones", "erasure_mentions")
STMTS = {"analyst_delete": "delete from analyst_jobs a where", "facts_delete": "delete from facts f where", "residue": "with fp as ("}
# control name -> (function, [(fixed fragment, pre-fix fragment), ...]); absent fixed fragments => control unavailable
FIX_APPLY = [(
    """      select a2.job_id from unnest(ids) x
        cross join lateral (select j2.job_id, j2.tenant_id from analyst_jobs j2 where j2.input_event_ids @> array[x] offset 0) a2
       where a2.tenant_id = t));""",
    """      select a2.job_id from unnest(ids) x join analyst_jobs a2 on a2.input_event_ids @> array[x] where a2.tenant_id = t));"""), (
    """      select f2.fact_id from unnest(ids) x
        cross join lateral (select g.fact_id, g.tenant_id from facts g where g.input_event_ids @> array[x] offset 0) f2
       where f2.tenant_id = t));""",
    """      select f2.fact_id from unnest(ids) x join facts f2 on f2.input_event_ids @> array[x] where f2.tenant_id = t));""")]
FIX_RESIDUE = [(
    """(select count(*) from (select event_id from fp
                                except select x.event_id from tombstones x where x.tenant_id = t and x.subject_id = s) u),""",
    """(select count(*) from fp
                                where not exists (select 1 from tombstones x where x.tenant_id = t and x.subject_id = s and x.event_id = fp.event_id)),"""), (
    """union select f.fact_id from tombstones x
                           cross join lateral (select g.fact_id, g.tenant_id from facts g where g.input_event_ids @> array[x.event_id] offset 0) f
                          where""",
    """union select f.fact_id from tombstones x join facts f on f.input_event_ids @> array[x.event_id]
                          where"""), (
    """from tombstones x
                          cross join lateral (select j2.job_id, j2.tenant_id from analyst_jobs j2 where j2.input_event_ids @> array[x.event_id] offset 0) a
                          where""",
    """from tombstones x join analyst_jobs a on a.input_event_ids @> array[x.event_id]
                          where""")]
CONTROLS = {"CTL_APPLY": ("wp_erasure_apply", FIX_APPLY), "CTL_RESIDUE": ("wp_erasure_pg_residue", FIX_RESIDUE)}
# amendment 18b: which states each control must be shown in (>= 2 of R runs); other states are recorded
CONTROL_STATES = {"CTL_APPLY": ("S1", "S2"), "CTL_RESIDUE": ("S1",)}
RESIDUE_PER_EVENT_MAX = 100
ROWS = re.compile(r"actual rows=(\d+) loops=(\d+)")
REMOVED = re.compile(r"Rows Removed by (Join Filter|Filter|Index Recheck): (\d+)")
INDEX = re.compile(r" on (\w+)(?: \w+)?  \(")


def work(plan):
    w = sum(int(r) * int(l) for r, l in ROWS.findall(plan)) + sum(int(n) for _, n in REMOVED.findall(plan))
    jf = max([int(n) for k, n in REMOVED.findall(plan) if k == "Join Filter"] or [0])
    return w, jf


def classify(note):
    m = note.split("Query Text:", 1)
    if len(m) < 2:
        return None
    q = " ".join(m[1].split())
    for k, head in STMTS.items():
        if " ".join(head.split()) in q[:400] or (k == "residue" and "with fp as (select event_id from events" in q):
            return k
    return None


def _append(conn, ks, t, subj, n, start=0, lease=None):
    s = lease or session(conn, t, ttl=600)          # one session lease per tenant: a second acquire while held is refused
    assert s is not None, f"no session lease for {t}"
    for i in range(0, n, 500):
        evs = [msg(start + i + j, f"{subj} note {start + i + j}", subj) for j in range(min(500, n - i))]
        r = log.append(conn, ks, s, evs)
        assert len(r["accepted"]) == len(evs), r
    return s


def one_run(state, m, patch=None):
    SETUP.main(True)                                       # amendment 18b: every run from a fresh database
    if patch:
        p = db.admin()
        for x in patch:
            p.execute(x)
        p.close()
    conn, ks = db.connect(), K.KeyService()
    adm = db.admin()
    try:
        for _ in range(SMALL_TENANTS):
            _append(conn, ks, tenant("pss"), "P", SMALL_EVENTS)
        if state == "S2":
            adm.execute("analyze")
            for tb in TABLES:
                adm.execute(f"alter table {tb} set (autovacuum_enabled = false)")
        t = tenant("psx")
        ls = _append(conn, ks, t, "X", m)
        _append(conn, ks, t, "Y", 5, start=m + 10, lease=ls)
        if state == "S1":
            adm.execute("analyze")
        jid = erasure.request(conn, t, "X")
        conn.execute("select wp_erasure_mark(%s)", (jid,))
        a = db.admin()                                    # fresh session: cold plpgsql plan cache, as a new worker
        notes = []
        a.add_notice_handler(lambda d: notes.append(d.message_primary or ""))
        a.execute("load 'auto_explain'")
        for k, v in (("auto_explain.log_min_duration", "0"), ("auto_explain.log_analyze", "on"), ("auto_explain.log_timing", "off"),
                     ("auto_explain.log_nested_statements", "on"), ("auto_explain.log_level", "notice"), ("client_min_messages", "notice")):
            a.execute(f"set {k} = '{v}'")
        batches = []
        while True:
            notes.clear()
            t0 = time.monotonic()
            n = a.execute("select wp_erasure_cascade(%s, %s)", (jid, BATCH)).fetchone()[0]
            dt = time.monotonic() - t0
            st = {}
            for note in notes:
                k = classify(note)
                if k in ("analyst_delete", "facts_delete"):
                    w, jf = work(note)
                    st[k] = {"work": w, "join_filter_max": jf, "indexes": sorted(set(INDEX.findall(note)))}
            if "analyst_delete" in st:
                batches.append({"rows": n, "s": round(dt, 4), **st})
            if not n:
                break
        notes.clear()
        t0 = time.monotonic()
        resid = a.execute("select wp_erasure_pg_residue(%s, 'X')", (t,)).fetchone()[0]
        rs = round(time.monotonic() - t0, 4)
        rn = [x for x in notes if classify(x) == "residue"]
        rw, rjf = work(rn[0]) if rn else (None, None)
        a.close()
        return {"state": state, "M": m, "tenant": t, "batches": batches, "residue": {"work": rw, "join_filter_max": rjf, "s": rs, "logged": len(rn),
                                                                                     "events_untombstoned": resid.get("pg:events_untombstoned")}}
    finally:
        if state == "S2":
            for tb in TABLES:
                adm.execute(f"alter table {tb} reset (autovacuum_enabled)")
        adm.close()
        conn.close()


def per_event(b, k):
    x = b.get(k)
    return x["work"] / max(1, b["rows"]) if x else 0.0


def gate_c(run):
    data = run["batches"]
    out = {}
    for k in ("analyst_delete", "facts_delete"):
        first = [per_event(b, k) for b in data[:5]]
        later = [per_event(b, k) for b in data[5:10]]
        f, l = (sum(first) / len(first) if first else 0.0), (sum(later) / len(later) if later else 0.0)
        out[k] = {"first_per_event": round(f, 2), "later_per_event": round(l, 2), "holds": bool(later) and l <= max(3 * f, 1.0) and l <= 50}
    return out


def measure(states, r, m1, m2, patch=None):
    runs = {s: {m1: [], m2: []} for s in states}
    for s in states:
        for i in range(r):
            for m in (m1, m2):
                x = one_run(s, m, patch)
                x["PS_C"] = gate_c(x)
                runs[s][m].append(x)
                print("plan_stability", s, m, i, json.dumps({k: v["later_per_event"] for k, v in x["PS_C"].items()}),
                      "residue", x["residue"]["work"], x["residue"]["s"], flush=True)
    viol = {}
    for s in states:
        for i in range(r):
            a, b = runs[s][m1][i], runs[s][m2][i]
            ratio = (b["residue"]["work"] / a["residue"]["work"]) if a["residue"]["work"] else None
            psr = ratio is not None and ratio <= 3 and all(x["residue"]["join_filter_max"] <= 10 * x["M"]
                                                           and x["residue"]["work"] <= RESIDUE_PER_EVENT_MAX * x["M"] for x in (a, b))
            psc = all(v["holds"] for x in (a, b) for v in x["PS_C"].values())
            b["PS_R"] = {"ratio": round(ratio, 2) if ratio else None, "holds": psr}
            viol.setdefault(s, []).append({"run": i, "PS_C": psc, "PS_R": psr})
    return runs, viol


def main(r, m1, m2):
    states = ("S1", "S2")
    out = {"params": {"R": r, "M": [m1, m2], "batch": BATCH, "small_tenants": SMALL_TENANTS, "small_events": SMALL_EVENTS}}
    runs, ok = measure(states, r, m1, m2)
    out["runs"], out["gates"] = runs, ok
    ctl = {}
    for name, (fn, frags) in CONTROLS.items():
        if not all(fx in function_sql(fn) for fx, _ in frags):
            ctl[name] = {"available": False, "why": "fixed fragment absent (pre-fix code)"}
            continue
        ap, rv = variant(fn, frags)                        # one replacement of the shipped body; every run resets afterwards
        cr, cv = measure(states, r, m1, m2, patch=ap)
        ctl[name] = {"available": True, "violated": {s: sum(1 for v in cv[s] if not (v["PS_C"] and v["PS_R"])) for s in states}, "gates": cv,
                     "function": fn, "required_states": list(CONTROL_STATES[name])}
    SETUP.main(True)                                       # leave the shipped schema in place
    out["controls"] = ctl
    why = [f"{s} run {v['run']}: PS_C {v['PS_C']} PS_R {v['PS_R']}" for s in states for v in ok[s] if not (v["PS_C"] and v["PS_R"])]
    for k, c in ctl.items():
        if not c["available"]:
            why.append(f"control {k} unavailable: {c['why']}")
        else:
            why += [f"control {k} {s}: violated {n}/{r} < 2" for s, n in c["violated"].items() if n < 2 and s in c["required_states"]]
    out["verdict"], out["why"] = ("FAIL" if why else "PASS"), why
    print("plan_stability", out["verdict"], json.dumps(ok), json.dumps({k: c.get("violated", "unavailable") for k, c in ctl.items()}), flush=True)
    write_result("plan_stability", out)


if __name__ == "__main__":
    kv = {"R": 3, "M1": 1500, "M2": 3000}
    for x in sys.argv[1:]:
        k, v = x.split("=", 1)
        if k not in kv:
            raise SystemExit(f"unknown parameter {k}; known {sorted(kv)}")
        kv[k] = int(v)
    main(kv["R"], kv["M1"], kv["M2"])
