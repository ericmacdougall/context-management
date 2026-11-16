"""B3: the decision core under real overlapping Postgres transactions (container emlab-pg-pol-16096 on t2-dev, database pol).
S1 two conflicting decisions -> contested; S2 stale token; S3 zombie (revoked while holding a prepared decision); S4 same idem key
concurrently; S5 resolve race. Overlap witness: a monitor connection sees the second transaction blocked on a lock while the first
holds its transaction open. Controls: blind last-writer-wins UPDATE; read-modify-write without a lock.
usage: python -m exp.b3_pg   (needs psycopg and the container; run on t2-dev)"""
import json
import threading
import time

from exp import common  # noqa: F401
from exp.common import write_result
from pol import pgcore
from pol.core import canon, current_table, genesis, idem_key, manifest_hash

R = 50
SCEN = ["s1", "s2", "s3", "s4", "s5"]
PRINCIPALS = {"u1": "human", "u2": "human", "a1": "agent"}
for _i in range(R):
    PRINCIPALS["z%d" % _i] = "human"
AUTHORITY = {}
for _s in SCEN + ["c1", "c2", "c3"]:
    for _i in range(R):
        AUTHORITY["%s_%d" % (_s, _i)] = ["u1", "u2"] + (["z%d" % _i] if _s == "s3" else [])
ADMINS = ["u1"]
TIMEOUT = 20.0


def E(type, id, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id)}
    d.update(kw)
    return d


def ts(c):
    return c.execute("select clock_timestamp()").fetchone()[0]


def wait_blocked(mon, pid, timeout=TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = mon.execute("select wait_event_type, clock_timestamp() from pg_stat_activity where pid=%s", (pid,)).fetchone()
        if r and r[0] == "Lock":
            return r[1]
        time.sleep(0.003)
    return None


def race(first, second, mon, pid_second):
    """first(hook) runs a transaction that holds its locks until the monitor sees `second` blocked; second(hook) is then started."""
    w, res, errs = {}, {}, []
    started = threading.Event()

    def hookA(name, c):
        if name == "locked":
            w["a_locked"] = ts(c)
            started.set()
            w["b_blocked"] = wait_blocked(mon, pid_second)
        elif name == "before_commit":
            w["a_before_commit"] = ts(c)

    def hookB(name, c):
        if name == "locked":
            w["b_locked"] = ts(c)

    def runA():
        try:
            res["a"] = first(hookA)
        except Exception as e:      # noqa: BLE001
            errs.append(("a", repr(e)))
            started.set()

    def runB():
        try:
            res["b"] = second(hookB)
        except Exception as e:      # noqa: BLE001
            errs.append(("b", repr(e)))

    ta = threading.Thread(target=runA)
    ta.start()
    started.wait(TIMEOUT)
    tb = threading.Thread(target=runB)
    tb.start()
    ta.join(TIMEOUT * 2)
    tb.join(TIMEOUT * 2)
    ok = all(k in w and w[k] is not None for k in ("a_locked", "b_blocked", "a_before_commit", "b_locked")) and w["a_locked"] < w["b_blocked"] < w["a_before_commit"] < w["b_locked"]
    return res, bool(ok), w, errs


def head(conn, req):
    return conn.execute("select doc from dc_reqs where req=%s", (req,)).fetchone()[0]


def seed_two_proposals(conn, req):
    pgcore.submit(conn, E("propose", req + "_P1", req=req, principal="u1", value="A", based_on=0))
    pgcore.submit(conn, E("propose", req + "_P2", req=req, principal="u2", value="B", based_on=0))


class Conns:
    def __init__(self):
        self.a, self.b, self.mon, self.adm = pgcore.connect(app="pol_a"), pgcore.connect(app="pol_b"), pgcore.connect(app="pol_mon"), pgcore.connect(app="pol_adm")
        self.pid_b = self.b.execute("select pg_backend_pid()").fetchone()[0]
        self.pid_a = self.a.execute("select pg_backend_pid()").fetchone()[0]


def s1(c, i):
    req = "s1_%d" % i
    seed_two_proposals(c.adm, req)
    swap = i % 2 == 1                           # who holds the lock first alternates
    ua, ub = ("u2", "u1") if swap else ("u1", "u2")
    pa, pb = (req + "_P2", req + "_P1") if swap else (req + "_P1", req + "_P2")
    ea = E("decide", req + "_Da", req=req, principal=ua, verb="accept", proposal=pa, channel="owner_ui", based_on=0)
    eb = E("decide", req + "_Db", req=req, principal=ub, verb="accept", proposal=pb, channel="owner_cli", based_on=0)
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, ea, h), lambda h: pgcore.submit(c.b, eb, h), c.mon, c.pid_b)
    d = head(c.adm, req)
    cands = sorted(x["event"] for x in (d["contested"] or {"candidates": []})["candidates"])
    ok = (res.get("a") == "decided_accept" and res.get("b") == "contested_opened" and d["status"] == "contested" and d["version"] == 2 and d["value"] is None
          and cands == sorted([ea["id"], eb["id"]]) and not errs)
    return ok, ov, {"codes": res, "status": d["status"], "version": d["version"], "errs": errs}


def s2(c, i):
    req = "s2_%d" % i
    seed_two_proposals(c.adm, req)
    pgcore.submit(c.adm, E("decide", req + "_D1", req=req, principal="u1", verb="accept", proposal=req + "_P1", channel="owner_ui", based_on=0))
    pgcore.submit(c.adm, E("supersede", req + "_S1", req=req, principal="u1", value="Z", channel="owner_ui", based_on=1))
    # head is now 2. A advances it to 3 while B (a stale token bound to version 0, and one bound to version 2) waits on the lock.
    ea = E("supersede", req + "_Sa", req=req, principal="u1", value="Y", channel="owner_ui", based_on=2)
    eb = E("supersede", req + "_Sb", req=req, principal="u2", value="STALE", channel="owner_cli", based_on=0)
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, ea, h), lambda h: pgcore.submit(c.b, eb, h), c.mon, c.pid_b)
    d = head(c.adm, req)
    ok = res.get("a") == "superseded" and res.get("b") == "stale" and d["version"] == 3 and d["value"] == "Y" and not errs
    return ok, ov, {"codes": res, "version": d["version"], "value": d["value"], "errs": errs}


def s3(c, i):
    req = "s3_%d" % i
    z = "z%d" % i
    pgcore.submit(c.adm, E("propose", req + "_P", req=req, principal="u1", value="A", based_on=0))
    prepared = E("supersede", req + "_Z", req=req, principal=z, value="ZOMBIE", channel="owner_ui", based_on=0)       # prepared while z still holds authority
    rev = E("revoke", req + "_REV", principal="u1", target=z)
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, rev, h), lambda h: pgcore.submit(c.b, prepared, h), c.mon, c.pid_b)
    d = head(c.adm, req)
    ok = res.get("a") == "revoked" and res.get("b") == "principal_revoked" and d["version"] == 0 and d["value"] is None and not errs
    return ok, ov, {"codes": res, "version": d["version"], "errs": errs}


def s4(c, i):
    req = "s4_%d" % i
    ev = E("supersede", req + "_X", req=req, principal="u1", value="V", channel="owner_ui", based_on=0)
    twin = dict(ev) if i % 2 == 0 else dict(ev, value="DIFFERENT")           # same payload -> duplicate; other payload -> idem_conflict
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, ev, h), lambda h: pgcore.submit(c.b, twin, h), c.mon, c.pid_b)
    d = head(c.adm, req)
    n = c.adm.execute("select count(*) from dc_events where idem=%s", (idem_key(ev),)).fetchone()[0]
    want = "duplicate" if i % 2 == 0 else "idem_conflict"
    ok = res.get("a") == "superseded" and res.get("b") == want and n == 1 and d["version"] == 1 and d["value"] == "V" and not errs
    return ok, ov, {"codes": res, "event_rows": n, "version": d["version"], "errs": errs}


def s5(c, i):
    req = "s5_%d" % i
    seed_two_proposals(c.adm, req)
    pgcore.submit(c.adm, E("decide", req + "_D1", req=req, principal="u1", verb="accept", proposal=req + "_P1", channel="owner_ui", based_on=0))
    pgcore.submit(c.adm, E("decide", req + "_D2", req=req, principal="u2", verb="accept", proposal=req + "_P2", channel="owner_cli", based_on=0))
    assert head(c.adm, req)["status"] == "contested"
    swap = i % 2 == 1
    ea = E("decide", req + "_Ra", req=req, principal="u1", verb="resolve", choose=req + "_D2" if swap else req + "_D1", channel="owner_ui", based_on=2)
    eb = E("decide", req + "_Rb", req=req, principal="u2", verb="resolve", choose=req + "_D1" if swap else req + "_D2", channel="owner_cli", based_on=2)
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, ea, h), lambda h: pgcore.submit(c.b, eb, h), c.mon, c.pid_b)
    d = head(c.adm, req)
    ok = res.get("a") == "resolved" and res.get("b") == "stale" and d["status"] == "current" and d["version"] == 3 and not errs
    return ok, ov, {"codes": res, "version": d["version"], "errs": errs}


def ctl_lww(c, i):
    """Control (a): blind last-writer-wins updates under the same forced overlap. The loss is silent: no refusal, no contested marker."""
    req = "c1_%d" % i
    res, ov, w, errs = race(lambda h: pgcore.submit_blind_lww(c.a, req, "A", "u1", h) or "wrote", lambda h: pgcore.submit_blind_lww(c.b, req, "B", "u2", h) or "wrote",
                            c.mon, c.pid_b)
    row = c.adm.execute("select value, decided_by, version from dc_lww where req=%s", (req,)).fetchone()
    silent = res.get("a") == "wrote" and res.get("b") == "wrote" and row[2] == 2 and row[0] == "B"     # both 'succeeded'; A's decision is gone, nothing flags it
    return silent, ov, {"final": row, "errs": errs}


def ctl_lww_engine(c, i):
    """Control (a): the engine with the sibling rule replaced by last-writer-wins (reducer flag), under the S1 overlap. Both decisions
    'apply', nothing is contested, and the log holds two applied decisions on the same base version."""
    req = "c3_%d" % i
    seed_two_proposals(c.adm, req)
    fl = frozenset(["lww"])
    ea = E("decide", req + "_Da", req=req, principal="u1", verb="accept", proposal=req + "_P1", channel="owner_ui", based_on=0)
    eb = E("decide", req + "_Db", req=req, principal="u2", verb="accept", proposal=req + "_P2", channel="owner_cli", based_on=0)
    res, ov, w, errs = race(lambda h: pgcore.submit(c.a, ea, h, fl), lambda h: pgcore.submit(c.b, eb, h, fl), c.mon, c.pid_b)
    d = head(c.adm, req)
    bases = [x["based_on"] for x in d["log"]]
    lost = (res.get("a") == "decided_accept" and res.get("b") == "decided_accept" and d["status"] == "current" and bases == [0, 0] and d["value"] == "B" and not errs)
    return lost, ov, {"codes": res, "log_bases": bases, "value": d["value"], "errs": errs}


def ctl_nolock(c, i):
    """Control (b): read-modify-write without a lock, forced interleaving read(A) read(B) write(A) write(B): lost update."""
    req = "c2_%d" % i
    seed_two_proposals(c.adm, req)
    both_read = [threading.Event(), threading.Event()]
    a_done = threading.Event()
    ea = E("decide", req + "_Da", req=req, principal="u1", verb="accept", proposal=req + "_P1", channel="owner_ui", based_on=0)
    eb = E("decide", req + "_Db", req=req, principal="u2", verb="accept", proposal=req + "_P2", channel="owner_cli", based_on=0)
    res, errs = {}, []

    def ha(name, conn):
        if name == "after_read":
            both_read[0].set()
            both_read[1].wait(TIMEOUT)

    def hb(name, conn):
        if name == "after_read":
            both_read[1].set()
            both_read[0].wait(TIMEOUT)
            a_done.wait(TIMEOUT)

    def ra():
        try:
            res["a"] = pgcore.submit_nolock(c.a, ea, ha)
        except Exception as e:      # noqa: BLE001
            errs.append(repr(e))
        a_done.set()

    def rb():
        try:
            res["b"] = pgcore.submit_nolock(c.b, eb, hb)
        except Exception as e:      # noqa: BLE001
            errs.append(repr(e))
    ta, tb = threading.Thread(target=ra), threading.Thread(target=rb)
    ta.start(); tb.start(); ta.join(TIMEOUT * 2); tb.join(TIMEOUT * 2)
    d = head(c.adm, req)
    codes = [r[0] for r in c.adm.execute("select code from dc_events where req=%s and ev->>'type'='decide' order by seq", (req,)).fetchall()]
    lost = codes == ["decided_accept", "decided_accept"] and d["version"] == 1 and not errs      # two 'applied' decisions on one version: one is lost
    return lost, True, {"codes": codes, "version": d["version"], "errs": errs}


def run_scenario(name, fn, conns):
    ok_n = ov_n = 0
    fails = []
    witnesses = []
    for i in range(R):
        try:
            ok, ov, info = fn(conns, i)
        except Exception as e:      # noqa: BLE001
            ok, ov, info = False, False, {"exception": repr(e)}
        ok_n += bool(ok)
        ov_n += bool(ov)
        if not ok and len(fails) < 3:
            fails.append({"round": i, **info})
        if i < 2:
            witnesses.append(info)
    return {"rounds": R, "passed": ok_n, "overlap_witnessed": ov_n, "failures_sample": fails, "sample": witnesses}


def main():
    c = Conns()
    g0 = pgcore.setup(c.adm, PRINCIPALS, AUTHORITY, ADMINS)
    out = {}
    for name, fn in [("S1_conflicting_decisions_contested", s1), ("S2_stale_token_refused", s2), ("S3_zombie_revoked_principal", s3), ("S4_same_idem_key_concurrent", s4),
                     ("S5_resolve_race", s5)]:
        out[name] = run_scenario(name, fn, c)
    ctl = {"ctl_engine_last_writer_wins": run_scenario("lww_engine", ctl_lww_engine, c), "ctl_blind_update_no_lock": run_scenario("lww", ctl_lww, c), "ctl_no_lock_read_modify_write": run_scenario("nolock", ctl_nolock, c)}
    # rebuild from the event table equals the live head rows, per requirement and as a manifest
    live = pgcore.read_state(c.adm)
    reb = pgcore.rebuild(c.adm, PRINCIPALS, AUTHORITY, ADMINS)
    rows = pgcore.log_rows(c.adm)
    per_req_equal, per_req_total = {}, {}
    for r in live["reqs"]:
        sc = r.split("_")[0]
        per_req_total[sc] = per_req_total.get(sc, 0) + 1
        per_req_equal[sc] = per_req_equal.get(sc, 0) + (canon(live["reqs"][r]) == canon(reb["reqs"][r]))
    auth_equal = canon({k: live[k] for k in ("authority", "revoked", "admins")}) == canon({k: reb[k] for k in ("authority", "revoked", "admins")})
    keep = lambda r: not r.startswith(("c2_", "c3_"))        # the no-lock control's requirements are expected to diverge from a rebuild
    live_f = dict(live, idem=reb["idem"], reqs={r: q for r, q in live["reqs"].items() if keep(r)})
    reb_f = dict(reb, reqs={r: q for r, q in reb["reqs"].items() if keep(r)})
    rows_f = [e for e in rows if not str(e.get("req", "")).startswith(("c2_", "c3_"))]
    man_equal = manifest_hash(live_f, rows_f) == manifest_hash(reb_f, rows_f)
    rebuild_rep = {"events_in_log": len(rows), "per_scenario_reqs_equal": per_req_equal, "per_scenario_reqs_total": per_req_total, "authority_equal": auth_equal, "manifest_equal": man_equal}
    ok = all(v["passed"] == R for v in out.values()) and all(out[k]["overlap_witnessed"] == R for k in ("S1_conflicting_decisions_contested", "S3_zombie_revoked_principal",
                                                                                                         "S5_resolve_race")) \
        and all(per_req_equal.get(s, 0) == per_req_total.get(s) for s in SCEN) and auth_equal and man_equal
    # controls: the controls' own requirements (c1, c2) are expected to diverge from a rebuild for the lost-update control
    ctl_ok = ctl["ctl_engine_last_writer_wins"]["passed"] >= 45 and ctl["ctl_engine_last_writer_wins"]["overlap_witnessed"] >= 45 and ctl["ctl_blind_update_no_lock"]["passed"] >= 45 and ctl["ctl_no_lock_read_modify_write"]["passed"] >= 45
    c2_mismatch = sum(1 for r in live["reqs"] if r.startswith("c2_") and canon(live["reqs"][r]) != canon(reb["reqs"][r]))
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    pgv = c.adm.execute("show server_version").fetchone()[0]
    data = {"verdict": verdict, "scenarios": out, "controls": ctl, "rebuild": rebuild_rep, "control_c2_live_vs_rebuild_mismatches": c2_mismatch, "pass_conditions_met": ok,
            "controls_meet_floor": ctl_ok,
            "not_proven": "single Postgres instance, READ COMMITTED, one writer pair per round (two overlapping transactions, not N-way); no process kill or network partition mid-transaction; "
                          "the events table has no hash chain enforced by the database; revoke locks every requirement row (a scaling limit)"}
    write_result("b3_pg", {"R": R, "scenarios": SCEN, "pg_port": pgcore.PG["port"]}, data, extra_env={"postgres": pgv, "container": "emlab-pg-pol-16096", "isolation": "read committed"})
    print(verdict, {k: (v["passed"], v["overlap_witnessed"]) for k, v in out.items()}, {k: (v["passed"], v["overlap_witnessed"]) for k, v in ctl.items()}, rebuild_rep)


if __name__ == "__main__":
    main()
