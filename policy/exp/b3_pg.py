"""B3: the decision core under real overlapping Postgres transactions (pol.pgcore on the experiment's own container, database pol,
connected as dc_app with a passfile). Env (paths only): POL_PG_PORT, POL_PG_SUPER_PASSFILE, POL_PG_APP_PASSWORD_FILE, POL_PG_APP_PASSFILE.

Scenarios (R rounds each, fresh requirement per round, two app connections plus a monitor):
  S1 two humans decide the same base differently -> contested (never one silent winner)
  S2 stale token while the head moves -> stale, state unchanged
  S3 zombie: revoke commits while a prepared decision of the revoked principal waits -> principal_revoked
  S4 twins: one principal, same idempotency key, same requirement, concurrently -> one event row; twin gets duplicate (same payload,
     with the ORIGINAL code) or idem_conflict (other payload)
  S4b twins on two DIFFERENT requirements (no shared head row): serialised by the per-principal advisory lock -> idem_conflict, one row
  S5 two resolves of a contested head -> one resolved, the other stale
  S6 tenant isolation: another tenant's admin cannot revoke or grant here; an event stamped with another tenant changes nothing
Overlap witness (S1, S3, S4, S4b, S5): the monitor sees the second backend waiting (wait_event_type = 'Lock') while the first holds its
transaction open, and the second acquires its locks only after the first reached commit.
Differential arm: random core_gen logs submitted to PgEngine and to MemoryState: codes and original codes equal per event, comparable
views equal at the end, the event table rebuilt in memory equals the live rows, dc.verify_chain ok, dc.audit rows == refused events.
Controls (one-patch variants of pol/pgcore.py, each with its own install): (a) head FOR UPDATE removed -> S1 silent winner;
(b) advisory lock removed -> S4b twin decided by the unique-index backstop (UniqueViolation retry) instead of the lock;
(c) advisory lock and unique idem index removed -> S4b both twins applied (two event rows, one idempotency key).
usage: python -m exp.b3_pg"""
import json
import os
import random
import threading
import time

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.core_gen import Gen, config as gen_config
from exp.patches import variant
from exp.seeds import seeds
import pol._core as CORE
from pol import pgcore as PG0

ROWS = []
R = int(os.environ.get("B3_ROUNDS", "50"))
DIFF_LOGS = int(os.environ.get("B3_DIFF_LOGS", "40"))
TIMEOUT = 20.0
T = "T1"
LAB = {"tenants": [T], "acl": [], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False, "restricted": False,
       "holds": [], "subjects": []}
SCEN = ["s1", "s2", "s3", "s4", "s5", "s4b", "c1", "c3"]


def cfg():
    pr = {"u1": "human", "u2": "human", "a1": "agent"}
    auth = {}
    for i in range(R):
        pr["z%d" % i] = "human"
        for s in SCEN:
            if s in ("s4b", "c3"):
                auth["%s_a%d" % (s, i)] = ["u1", "u2"]
                auth["%s_b%d" % (s, i)] = ["u1", "u2"]
            else:
                auth["%s_%d" % (s, i)] = ["u1", "u2"] + (["z%d" % i] if s == "s3" else [])
    return {"tenant": T, "principals": pr, "authority": auth, "admins": ["u1"]}


T2CFG = {"tenant": "T2", "principals": {"x1": "human", "u1": "human"}, "authority": {"t2r": ["x1"]}, "admins": ["x1"]}


def env():
    return int(os.environ["POL_PG_PORT"]), os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], os.environ["POL_PG_APP_PASSFILE"]


def E(type, id, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id), "tenant": kw.pop("tenant", T)}
    pid = kw.get("principal")
    d["channel"] = kw.pop("channel", "owner_ui")
    if type in ("propose", "supersede") or (type == "decide" and kw.get("verb") == "amend"):
        d["label"] = dict(LAB)
    d.update(kw)
    if d.get("proposal") and "proposal_hash" not in d:
        ref, author = PROPS[d["proposal"]]
        d["proposal_hash"] = CORE.proposal_hash(d["proposal"], ref, author)
    _ = pid
    return d


PROPS = {}


def ts(c):
    return c.execute("select clock_timestamp()").fetchone()[0]


def wait_blocked(mon, pid, timeout=TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = mon.execute("select wait_event_type, wait_event, clock_timestamp() from pg_stat_activity where pid=%s", (pid,)).fetchone()
        if r and r[0] == "Lock":
            return r[2], r[1]
        time.sleep(0.002)
    return None, None


class Conns:
    def __init__(self, pg):
        port, _s, _a, ap = env()
        self.pg = pg
        self.a, self.b, self.mon, self.adm = (pg.connect_app(port, ap, n) for n in ("b3_a", "b3_b", "b3_mon", "b3_adm"))
        self.pid_b = self.b.execute("select pg_backend_pid()").fetchone()[0]
        self.ea, self.eb, self.eadm = pg.PgEngine(self.a, T), pg.PgEngine(self.b, T), pg.PgEngine(self.adm, T)

    def close(self):
        for c in (self.a, self.b, self.mon, self.adm):
            c.close()


def race(c, ev_a, ev_b, wait_at="locked"):
    """A runs to `wait_at` and holds its transaction until the monitor sees B waiting on a lock; then A commits and B proceeds."""
    w, res, errs = {}, {}, []
    started = threading.Event()

    def hook_a(name):
        if name == "locked":
            w["a_locked"] = ts(c.a)
            started.set()
        if name == wait_at:
            w["b_blocked"], w["b_wait_event"] = wait_blocked(c.mon, c.pid_b)
        if name == "before_commit":
            w["a_before_commit"] = ts(c.a)

    def hook_b(name):
        if name == "locked":
            w["b_locked"] = ts(c.b)

    def run(key, eng, ev, hook):
        try:
            res[key] = eng._submit(ev, hook)
        except Exception as e:      # noqa: BLE001
            errs.append((key, repr(e)))
            started.set()
    ta = threading.Thread(target=run, args=("a", c.ea, ev_a, hook_a))
    ta.start()
    started.wait(TIMEOUT)
    tb = threading.Thread(target=run, args=("b", c.eb, ev_b, hook_b))
    tb.start()
    ta.join(TIMEOUT * 3)
    tb.join(TIMEOUT * 3)
    ks = ("a_locked", "b_blocked", "a_before_commit", "b_locked")
    if wait_at == "locked":
        ov = all(w.get(k) is not None for k in ks) and w["a_locked"] < w["b_blocked"] < w["a_before_commit"] < w["b_locked"]
    else:
        ov = all(w.get(k) is not None for k in ks[:3]) and w["a_locked"] < w["b_blocked"] < w["a_before_commit"]
    return res, bool(ov), {k: (str(v) if v is not None else None) for k, v in w.items()}, errs


def head(c, req):
    return c.eadm._head(req)


def seed_two(c, req):
    for pid, n, ref in (("u1", "P1", "vA"), ("u2", "P2", "vB")):
        ev = E("propose", req + "_" + n, req=req, principal=pid, value_ref=ref, based_on=0)
        PROPS[ev["id"]] = (ref, pid)
        assert c.eadm._submit(ev)[0] == "proposed"


def s1(c, i, req=None):
    req = req or "s1_%d" % i
    seed_two(c, req)
    swap = i % 2 == 1
    ua, ub = ("u2", "u1") if swap else ("u1", "u2")
    pa, pb = (req + "_P2", req + "_P1") if swap else (req + "_P1", req + "_P2")
    ea = E("decide", req + "_Da", req=req, principal=ua, verb="accept", proposal=pa, based_on=0)
    eb = E("decide", req + "_Db", req=req, principal=ub, verb="accept", proposal=pb, channel="owner_cli", based_on=0)
    return ea, eb


def check_s1(c, i, res, req=None):
    req = req or "s1_%d" % i
    d = head(c, req)
    cands = sorted(x["event"] for x in (d["contested"] or {"candidates": []})["candidates"])
    ok = (res.get("a") == ("decided_accept", None) and res.get("b") == ("contested_opened", None) and d["status"] == "contested" and d["version"] == 2
          and d["value_ref"] is None and cands == sorted([req + "_Da", req + "_Db"]))
    return ok, {"status": d["status"], "version": d["version"], "value_ref": d["value_ref"], "candidates": cands}


def run_s1(c, i):
    ea, eb = s1(c, i)
    res, ov, w, errs = race(c, ea, eb)
    ok, info = check_s1(c, i, res)
    return ok and not errs, ov, dict(info, codes=res, w=w, errs=errs)


def run_s2(c, i):
    req = "s2_%d" % i
    seed_two(c, req)
    c.eadm._submit(E("decide", req + "_D1", req=req, principal="u1", verb="accept", proposal=req + "_P1", based_on=0))
    c.eadm._submit(E("supersede", req + "_S1", req=req, principal="u1", value_ref="vZ", based_on=1))
    ea = E("supersede", req + "_Sa", req=req, principal="u1", value_ref="vY", based_on=2)
    eb = E("supersede", req + "_Sb", req=req, principal="u2", value_ref="vSTALE", channel="owner_cli", based_on=0)
    res, ov, w, errs = race(c, ea, eb)
    d = head(c, req)
    ok = res.get("a") == ("superseded", None) and res.get("b") == ("stale", None) and d["version"] == 3 and d["value_ref"] == "vY" and not errs
    return ok, ov, {"codes": res, "version": d["version"], "value_ref": d["value_ref"], "w": w, "errs": errs}


def run_s3(c, i):
    req, z = "s3_%d" % i, "z%d" % i
    rev = E("revoke", req + "_REV", principal="u1", target=z)
    prepared = E("supersede", req + "_Z", req=req, principal=z, value_ref="vZOMBIE", based_on=0)
    res, ov, w, errs = race(c, rev, prepared)
    d = head(c, req)
    ok = res.get("a") == ("revoked", None) and res.get("b") == ("principal_revoked", None) and d["version"] == 0 and d["value_ref"] is None and not errs
    return ok, ov, {"codes": res, "version": d["version"], "w": w, "errs": errs}


def run_s4(c, i):
    req = "s4_%d" % i
    ev = E("supersede", req + "_X", req=req, principal="u1", value_ref="vV", based_on=0)
    twin = dict(ev) if i % 2 == 0 else dict(ev, value_ref="vDIFFERENT")
    res, ov, w, errs = race(c, ev, twin)
    n = c.adm.execute("select count(*) from dc.events where tenant=%s and principal='u1' and idem=%s", (T, ev["idem"])).fetchone()[0]
    want = ("duplicate", "superseded") if i % 2 == 0 else ("idem_conflict", "superseded")
    d = head(c, req)
    ok = res.get("a") == ("superseded", None) and res.get("b") == want and n == 1 and d["version"] == 1 and d["value_ref"] == "vV" and not errs
    return ok, ov, {"codes": res, "event_rows": n, "w": w, "errs": errs}


def s4b_events(i, pre="s4b"):
    ra, rb = "%s_a%d" % (pre, i), "%s_b%d" % (pre, i)
    ea = E("supersede", ra + "_X", req=ra, principal="u1", value_ref="vV", based_on=0, idem="%s-shared-%d" % (pre, i))
    eb = E("supersede", rb + "_X", req=rb, principal="u1", value_ref="vV", based_on=0, idem="%s-shared-%d" % (pre, i))
    return ra, rb, ea, eb


def run_s4b(c, i):
    ra, rb, ea, eb = s4b_events(i)
    before = c.pg.BACKSTOP[0]
    res, ov, w, errs = race(c, ea, eb)
    n = c.adm.execute("select count(*) from dc.events where tenant=%s and principal='u1' and idem=%s", (T, ea["idem"])).fetchone()[0]
    ok = res.get("a") == ("superseded", None) and res.get("b") == ("idem_conflict", "superseded") and n == 1 and head(c, rb)["version"] == 0 and not errs
    return ok, ov, {"codes": res, "event_rows": n, "backstop_retries": c.pg.BACKSTOP[0] - before, "wait_event": w.get("b_wait_event"), "w": w, "errs": errs}


def run_s5(c, i):
    req = "s5_%d" % i
    seed_two(c, req)
    c.eadm._submit(E("decide", req + "_D1", req=req, principal="u1", verb="accept", proposal=req + "_P1", based_on=0))
    c.eadm._submit(E("decide", req + "_D2", req=req, principal="u2", verb="accept", proposal=req + "_P2", channel="owner_cli", based_on=0))
    assert head(c, req)["status"] == "contested"
    swap = i % 2 == 1
    ea = E("decide", req + "_Ra", req=req, principal="u1", verb="resolve", choose=req + ("_D2" if swap else "_D1"), based_on=2)
    eb = E("decide", req + "_Rb", req=req, principal="u2", verb="resolve", choose=req + ("_D1" if swap else "_D2"), channel="owner_cli", based_on=2)
    res, ov, w, errs = race(c, ea, eb)
    d = head(c, req)
    ok = res.get("a") == ("resolved", None) and res.get("b") == ("stale", None) and d["status"] == "current" and d["version"] == 3 and not errs
    return ok, ov, {"codes": res, "version": d["version"], "w": w, "errs": errs}


def run_s6(c, i):
    before = json.dumps(c.eadm._comparable(), sort_keys=True)
    c1 = c.eadm._submit(E("revoke", "t2rev%d" % i, principal="x1", target="u2"))
    c2 = c.eadm._submit(E("revoke", "t2rev2_%d" % i, principal="u1", target="u2", tenant="T2"))
    c3 = c.eadm._submit(E("grant", "t2g%d" % i, principal="x1", req="s1_0", target="x1", role="decide"))
    after = json.dumps(c.eadm._comparable(), sort_keys=True)
    ok = c1[0] == "unauthorized" and c2[0] == "tenant_mismatch" and c3[0] == "unauthorized" and before == after
    return ok, True, {"codes": [c1, c2, c3]}


# ------------------------------------------------------------------------------------------------ controls
def ctl_no_head_lock(c, i):
    req = "c1_%d" % i
    ea, eb = s1(c, i, req)
    res, ov, w, errs = race(c, ea, eb, wait_at="before_commit")
    d = head(c, req)
    silent = (res.get("a") == ("decided_accept", None) and res.get("b") == ("decided_accept", None) and d["status"] == "current" and d["contested"] is None)
    return silent, ov, {"codes": res, "status": d["status"], "version": d["version"], "w": w, "errs": errs}


def ctl_no_advisory(c, i):
    ra, rb, ea, eb = s4b_events(i, "c3")
    before = c.pg.BACKSTOP[0]
    res, ov, w, errs = race(c, ea, eb, wait_at="before_commit")
    retries = c.pg.BACKSTOP[0] - before
    return retries >= 1, ov, {"codes": res, "backstop_retries": retries, "wait_event": w.get("b_wait_event"), "w": w, "errs": errs}


def ctl_idem_unprotected(c, i):
    """Nothing makes B wait here (no shared row, no advisory lock, no unique index), so the overlap is forced with events instead of a
    lock: A holds its transaction open at before_commit until B has computed its outcome and reached its own before_commit."""
    ra, rb, ea, eb = s4b_events(i, "c3")
    res, errs, w = {}, [], {}
    a_locked, b_ready = threading.Event(), threading.Event()

    def hook_a(name):
        if name == "locked":
            a_locked.set()
        if name == "before_commit":
            w["b_ready_while_a_open"] = b_ready.wait(TIMEOUT)

    def hook_b(name):
        if name == "before_commit":
            b_ready.set()

    def run(key, eng, ev, hook):
        try:
            res[key] = eng._submit(ev, hook)
        except Exception as e:      # noqa: BLE001
            errs.append((key, repr(e)))
            a_locked.set()
            b_ready.set()
    ta = threading.Thread(target=run, args=("a", c.ea, ea, hook_a))
    ta.start()
    a_locked.wait(TIMEOUT)
    tb = threading.Thread(target=run, args=("b", c.eb, eb, hook_b))
    tb.start()
    ta.join(TIMEOUT * 3)
    tb.join(TIMEOUT * 3)
    ov = bool(w.get("b_ready_while_a_open"))
    n = c.adm.execute("select count(*) from dc.events where tenant=%s and principal='u1' and idem=%s", (T, ea["idem"])).fetchone()[0]
    both = res.get("a") == ("superseded", None) and res.get("b") == ("superseded", None) and n == 2
    return both, True, {"codes": res, "event_rows": n, "w": w, "errs": errs}


PG_CONTROLS = {
    "no_head_lock": ([("            c.execute(\"select 1 from dc.head where tenant=%s and req=%s for update\", (t, ev[\"req\"]))\n", "            pass\n")], ctl_no_head_lock),
    "no_advisory_lock": ([("            c.execute(\"select pg_advisory_xact_lock(hashtextextended(%s, 0))\", (json.dumps([t, ev[\"principal\"]]),))\n", "            pass\n")],
                         ctl_no_advisory),
    "idem_unprotected": ([("            c.execute(\"select pg_advisory_xact_lock(hashtextextended(%s, 0))\", (json.dumps([t, ev[\"principal\"]]),))\n", "            pass\n"),
                          ("create unique index events_idem on dc.events(tenant, principal, idem);\n", "")], ctl_idem_unprotected),
}


def pg_variant(name, patches):
    import sys
    import types
    path = PG0.__file__
    src = open(path, encoding="utf-8").read()
    for old, new in patches:
        assert src.count(old) == 1, (name, old)
        src = src.replace(old, new)
    mod = types.ModuleType("pol.pgcore__b3_" + name)
    mod.__file__, mod.__package__ = path + "#" + name, "pol"
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def install(pg, tenants):
    port, sp, apw, _ap = env()
    pg.install(port, sp, apw, tenants)


def rounds(name, fn, c):
    ok_n = ov_n = 0
    fails = []
    for i in range(R):
        try:
            ok, ov, info = fn(c, i)
        except Exception as e:      # noqa: BLE001
            ok, ov, info = False, False, {"exception": repr(e)}
        ROWS.append({"kind": "round", "scenario": name, "round": i, "ok": bool(ok), "overlap": bool(ov), "info": info})
        ok_n += bool(ok)
        ov_n += bool(ov)
        if not ok and len(fails) < 3:
            fails.append({"round": i, **info})
    return {"rounds": R, "passed": ok_n, "overlap_witnessed": ov_n, "failures_sample": fails}


def fold(rows_ev, config):
    st = CORE.MemoryState(config)
    for ev in rows_ev:
        _c, _o, d = CORE.reduce(st, ev)
        st.apply_delta(d)
    return st


def differential(seed_list):
    """Random core_gen logs on PgEngine vs MemoryState (one install per log)."""
    port, _sp, _apw, ap = env()
    out = {"logs": 0, "events": 0, "mismatch_events": 0, "view_mismatch_logs": 0, "rebuild_mismatch_logs": 0, "chain_bad_logs": 0, "audit_bad_logs": 0,
           "codes": {}}
    per = max(1, DIFF_LOGS // len(seed_list))
    for s in seed_list:
        for li in range(per):
            rng = random.Random("%s-b3diff-%d" % (s, li))
            g = Gen("%s-b3diff-%d" % (s, li))
            config = gen_config()
            install(PG0, {config["tenant"]: config})
            conn = PG0.connect_app(port, ap, "b3_diff")
            eng = PG0.PgEngine(conn, config["tenant"])
            st = CORE.MemoryState(config)
            hist, refused = [], 0
            n = rng.randint(50, 300)
            mism = 0
            for _ in range(n):
                ev = g.retry_or_new(hist)
                hist.append(ev)
                mc, mo, d = CORE.reduce(st, ev)
                st.apply_delta(d)
                pc, po = eng._submit(ev)
                refused += pc in CORE.REFUSED
                out["codes"][pc] = out["codes"].get(pc, 0) + 1
                if (mc, mo) != (pc, po):
                    mism += 1
                ROWS.append({"kind": "diff_event", "log": "%s/%d" % (s, li), "ev": ev, "mem": [mc, mo], "pg": [pc, po]})
                for r in ("r1", "r2"):
                    g.note_version(r, st.heads[r]["version"])
            pv, mv = eng._comparable(), st.comparable()
            logged = [e for (e,) in conn.execute("select ev from dc.events where tenant=%s order by chain_n", (config["tenant"],)).fetchall()]
            rb = fold(logged, config).comparable()
            chain = tuple(eng._verify_chain())
            audit_n = conn.execute("select count(*) from dc.audit where tenant=%s", (config["tenant"],)).fetchone()[0]
            row = {"kind": "diff_log", "log": "%s/%d" % (s, li), "events": n, "mismatch_events": mism, "pg_view": pv, "mem_view": mv, "rebuild_view": rb,
                   "chain": list(chain), "applied_rows": len(logged), "audit_rows": audit_n, "refused": refused}
            ROWS.append(row)
            conn.close()
            out["logs"] += 1
            out["events"] += n
            out["mismatch_events"] += mism
            out["view_mismatch_logs"] += pv != mv
            out["rebuild_mismatch_logs"] += rb != pv
            out["chain_bad_logs"] += not (chain[1] and chain[0] == len(logged))
            out["audit_bad_logs"] += audit_n != refused
    return out


def main():
    t0 = time.time()
    port, sp, _apw, ap = env()
    sd = seeds("b3")
    # differential arm first (it reinstalls per log)
    diff = differential(sd)
    # scenarios on one install
    install(PG0, {T: cfg(), "T2": T2CFG})
    c = Conns(PG0)
    out = {}
    for name, fn in [("S1_conflicting_decisions_contested", run_s1), ("S2_stale_token_refused", run_s2), ("S3_zombie_revoked_principal", run_s3),
                     ("S4_same_idem_key_concurrent", run_s4), ("S4b_twins_on_two_requirements", run_s4b), ("S5_resolve_race", run_s5),
                     ("S6_tenant_isolation", run_s6)]:
        out[name] = rounds(name, fn, c)
    # rebuild of the whole scenario tenant from its event table, chain and audit
    logged = [e for (e,) in c.adm.execute("select ev from dc.events where tenant=%s order by chain_n", (T,)).fetchall()]
    live = c.eadm._comparable()
    reb = fold(logged, cfg()).comparable()
    chain = tuple(c.eadm._verify_chain())
    refused_logged = c.adm.execute("select count(*) from dc.audit where tenant=%s", (T,)).fetchone()[0]
    ROWS.append({"kind": "scen_rebuild", "live": live, "rebuild": reb, "chain": list(chain), "applied_rows": len(logged), "audit_rows": refused_logged})
    c.close()
    pgv = None
    # controls, each with its own install of its variant schema
    ctl = {}
    for name, (patches, fn) in PG_CONTROLS.items():
        mod = pg_variant(name, patches)
        install(mod, {T: cfg()})
        cc = Conns(mod)
        if pgv is None:
            pgv = cc.adm.execute("show server_version").fetchone()[0]
        ctl[name] = rounds("ctl_" + name, fn, cc)
        cc.close()
    scen_ok = all(v["passed"] == R for v in out.values())
    ov_ok = all(out[k]["overlap_witnessed"] == R for k in ("S1_conflicting_decisions_contested", "S3_zombie_revoked_principal", "S4_same_idem_key_concurrent",
                                                            "S4b_twins_on_two_requirements", "S5_resolve_race"))
    diff_ok = (diff["mismatch_events"] == 0 and diff["view_mismatch_logs"] == 0 and diff["rebuild_mismatch_logs"] == 0 and diff["chain_bad_logs"] == 0
               and diff["audit_bad_logs"] == 0 and diff["logs"] > 0)
    reb_ok = live == reb and chain[1] and chain[0] == len(logged)
    ok = scen_ok and ov_ok and diff_ok and reb_ok
    ctl_ok = all(v["passed"] >= int(0.9 * R) for v in ctl.values()) and ctl["no_head_lock"]["overlap_witnessed"] >= int(0.9 * R)
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "scenarios": out, "differential": diff, "scenario_rebuild_equal": live == reb, "scenario_chain": list(chain),
            "controls": ctl, "pass_conditions_met": ok, "controls_meet_floor": ctl_ok, "backstop_total": PG0.BACKSTOP[0], "seconds": round(time.time() - t0, 1),
            "not_proven": "single Postgres 16 instance, READ COMMITTED, two overlapping writers per round (not N-way); no process kill or network partition "
                          "mid-transaction; revoke takes the tenant row FOR UPDATE and so serialises the whole tenant (a scaling limit)"}
    data["items"] = dict(zip(("file", "sha256"), write_items("b3_pg", ROWS)))
    write_result("b3_pg", {"R": R, "diff_logs": diff["logs"], "seeds": sd, "pg_port": port}, data,
                 extra_env={"postgres": pgv, "container": "emlab-pg-polr-16096", "isolation": "read committed", "role": "dc_app"})
    print(verdict, {k: (v["passed"], v["overlap_witnessed"]) for k, v in out.items()}, diff, {k: (v["passed"], v["overlap_witnessed"]) for k, v in ctl.items()},
          "rebuild", live == reb, "chain", chain)


if __name__ == "__main__":
    main()
