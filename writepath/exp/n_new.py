"""N1-N7 on the NEW write path, each with a broken-variant control in the same harness (thresholds: exp/THRESHOLDS.md).
usage: python -m exp.n_new [R] [N1 N2 ...]"""
import json
import subprocess
import sys
import threading

from exp.common import write_result
from exp.util import env, tenant, msg, fact, session, es_count_all, es_count_live, drop_tenant
from wp import db, erasure, families as F, keys as K, lease as L, log, projector, reader, registry
from wp.setup import CTL_DB

CLOCK = db.connect(application_name="wp-clock")


def now():
    return CLOCK.execute("select clock_timestamp()").fetchone()[0]


def Xq(subj="X"):
    return {"term": {"subject_id": subj}}


# ------------------------------------------------------------------ N1
def n1(arm):
    """Projector reads X's new event (plaintext decoded), then the whole erasure of X runs, then the projector writes."""
    conn, ks, es = env()
    t = tenant("n1")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "hello zqn1y", "Y"), msg(1, "X says zqn1a", "X")])
    p = projector.Projector(conn, ks, es, t)
    p.run()
    log.append(conn, ks, s, [msg(2, "X later says zqn1b", "X")])
    w = {}

    def hook(name, data=None):
        if name == "mirror_after_read" and "erase" not in w:
            w["rows_with_X"] = sum(1 for r in data if r[4] == "X")
            w["t_read"] = now()
            w["erase"] = erasure.erase(db.connect(), ks, es, t, "X")
            w["t_erase_done"] = now()
            if arm == "control":                        # BROKEN: resolve the epoch AFTER the read (old N1 shape)
                p.lease = L.acquire(conn, t, L.PROJECTOR, 30, force=True)
                F.fence_es(es, t, p.lease.epoch)
        if name == "mirror_before_bulk" and "t_write" not in w:
            w["t_write"] = now()
    p.hook = hook
    n = p.step()
    p.hook = lambda *a: None
    p.run()                                              # liveness: the projector recovers and catches up
    leak = es_count_all(es, t, Xq())
    y_live = es_count_live(es, t, "events", Xq("Y"))
    holds = leak == 0 and w["erase"]["done"]
    return holds, ("X leaked into ES after erasure done" if leak else ("job not done" if not holds else "")), {
        "rows_read_with_X": w["rows_with_X"], "overlap": w["t_read"] < w["t_erase_done"] < w["t_write"], "step_result": n,
        "refused_bulk_items": p.stats["stale_bulk_items"], "X_docs_all_indices": leak, "Y_docs_live_after_recovery": y_live, "erase_done": w["erase"]["done"]}


# ------------------------------------------------------------------ N2
def n2(arm):
    conn, ks, es = env()
    t = tenant("n2")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "hello zqn2y", "Y"), msg(1, "X says zqn2x", "X"), fact(2, "X", "Xavier", "diagnosis", "zqn2flu")])
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()
    rogue = f"{F.P}{t}-ev2-base"
    if arm == "control":                                 # BROKEN: a writer for a family that is not registered
        es.req("PUT", f"/{rogue}", {"settings": F.SETTINGS, "mappings": {"properties": {"subject_id": {"type": "keyword"}, "content": {"type": "text"}}}})
        es.req("PUT", f"/{rogue}/_doc/x1", {"subject_id": "X", "content": "X says zqn2x"}, refresh="true")
    before = {f: es_count_live(es, t, f, Xq()) for f in F.FAMILIES}
    unreg_before = [u for u in registry.unregistered(conn, ks.conn, es) if t in u]
    rec = erasure.erase(conn, ks, es, t, "X")
    after_all = es_count_all(es, t, Xq())
    holds = after_all == 0 and rec["done"] and not unreg_before
    if arm == "control":
        es.req("DELETE", f"/{rogue}", ok=(200, 404))
    return holds, ("residue in an unregistered family" if after_all else "") + (" flagged:" + ",".join(unreg_before) if unreg_before else ""), {
        "families_enumerated": len(F.FAMILIES), "X_docs_before_per_family": before, "X_docs_all_indices_after": after_all,
        "receipt_done": rec["done"], "receipt_verify": rec["verify"], "unregistered_flagged": unreg_before}


# ------------------------------------------------------------------ N3
def n3(arm):
    conn, ks, es = env()
    t = tenant("n3")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X says zqn3x", "X"), msg(1, "Y says zqn3y", "Y"), msg(2, "Z says hello", "Z")])
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()
    w = {}

    def hook(name, data=None):
        if name == "rebuild_after_read:events" and "B" not in w:
            w["A_read_has_Y"] = any(r[4] == "Y" for r in data)
            w["t_A_read"] = now()
            w["B"] = erasure.erase(db.connect(), ks, es, t, "Y")
            w["t_B_done"] = now()
            if arm == "control":                         # BROKEN: A writes its survivors by concrete index name
                live = [i for i, a in es.alias_map(f"{F.P}{t}-events-*").items() if F.read_alias(t, "events") in a][0]
                nd = ""
                for r in data:
                    nd += json.dumps({"index": {"_index": live, "_id": r[0]}}) + "\n" + json.dumps({"event_id": r[0], "subject_id": r[4], "content": "Y says zqn3y" if r[4] == "Y" else "x"}) + "\n"
                es.req("POST", "/_bulk", ndjson=nd, refresh="true")
            w["t_A_write"] = now()
    jA = erasure.request(conn, t, "X")
    recA = erasure.run(db.connect(), ks, es, jA, hook=hook)
    y_any = es_count_all(es, t, Xq("Y"))
    sweep = erasure.sweep(conn, ks, es)
    x_any = es_count_all(es, t, Xq("X"))
    holds = y_any == 0 and w["B"]["done"]
    return holds, ("Y leaked after B done" if y_any else ""), {
        "A_survivor_read_had_Y": w["A_read_has_Y"], "overlap": w["t_A_read"] < w["t_B_done"] <= w["t_A_write"], "B_done": w["B"]["done"],
        "A_done_first_try": recA["done"], "A_error": recA["error_type"], "Y_docs_all_indices": y_any, "sweep": sweep, "X_docs_after_sweep": x_any}


# ------------------------------------------------------------------ N3 at the swap: takeover between A's bulk and A's swap
def n3s(arm, taker):
    conn, ks, es = env()
    t = tenant("n3s")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X says zqn3x", "X"), msg(1, "Y says zqn3y", "Y"), msg(2, "Z says zqn3z", "Z")])
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()
    w = {}
    p2 = None

    def hook(name, data=None):
        nonlocal p2
        if name == "rebuild_before_swap:events" and "taken" not in w:
            stage = [(i, a) for i, al in es.alias_map(f"{F.P}{t}-events-*").items() for a in al if a.startswith(f"{F.P}{t}-events-s")]
            w["A_staging_has_Y"] = es.count(stage[0][0], Xq("Y")) if stage else None
            w["t_A_bulk_done"] = now()
            if taker == "eraser":
                w["taken"] = erasure.erase(db.connect(), ks, es, t, "Y")["done"]
            else:
                p2 = projector.Projector(db.connect(), K.KeyService(), es, t)
                w["taken"] = p2.start(force=True)
            w["t_taken"] = now()
            if arm == "control" and stage:                  # BROKEN: A swaps without the compare-and-set (no must_exist anchor)
                si, sa = stage[0]
                others = [i for i in F._family_state(es, t, "events") if i != si]
                try:
                    es.aliases([{"remove_index": {"index": i}} for i in others] +
                               [{"add": {"index": si, "alias": F.read_alias(t, "events")}}, {"add": {"index": si, "alias": sa.replace("-events-s", "-events-w"), "is_write_index": True}}])
                    w["control_swapped"] = True
                except Exception as e:  # noqa: BLE001
                    w["control_swapped"] = f"not applicable: {type(e).__name__}"
            w["t_A_swap_attempt"] = now()
    jA = erasure.request(conn, t, "X")
    recA = erasure.run(db.connect(), ks, es, jA, hook=hook)
    out = {"A_done_first_try": recA["done"], "A_error": recA["error_type"]}
    if taker == "projector":
        log.append(conn, ks, s, [msg(3, "W says zqn3w after takeover", "W")])
        p2.run()
        out["W_live"] = es_count_live(es, t, "events", Xq("W"))
        out["p2_write_alias_present"] = any(F.write_alias(t, "events", p2.lease.epoch) in al for al in es.alias_map(f"{F.P}{t}-events-*").values()) if p2.lease else False
        p2.stop()
    live = [i for i, a in es.alias_map(f"{F.P}{t}-events-*").items() if F.read_alias(t, "events") in a]
    out.update({"read_alias_indices": len(live), "Y_live": es_count_live(es, t, "events", Xq("Y")), "Z_live": es_count_live(es, t, "events", Xq("Z"))})
    out["sweep"] = erasure.sweep(conn, ks, es)
    out["Y_all_after_sweep"] = es_count_all(es, t, Xq("Y"))
    out.update({k: w.get(k) for k in ("A_staging_has_Y", "taken", "control_swapped")})
    out["overlap"] = bool(w) and w["t_A_bulk_done"] < w["t_taken"] <= w["t_A_swap_attempt"]
    if taker == "eraser":
        holds = out["Y_live"] == 0 and out["read_alias_indices"] == 1 and out["Z_live"] == 1 and out["Y_all_after_sweep"] == 0
    else:
        holds = out["W_live"] == 1 and out["p2_write_alias_present"] and out["read_alias_indices"] == 1 and out["Z_live"] == 1
    return holds, "" if holds else "stale swap took effect", out


def n3s_eraser(arm):
    return n3s(arm, "eraser")


def n3s_projector(arm):
    return n3s(arm, "projector")


# ------------------------------------------------------------------ N8: writers racing phase 1 (bump-first ordering + erased-subject guards)
def n8(arm):
    dbname = CTL_DB if arm == "control" else None
    mk = (lambda n: db.connect(db=dbname, application_name=n)) if dbname else (lambda n: db.connect(application_name=n))
    conn, ks, es = env(dbname)
    t = tenant("n8")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X says zqn8x", "X"), msg(1, "Y says zqn8y", "Y")])
    p = projector.Projector(mk("n8-derive"), K.KeyService(), es, t)
    p.run()
    log.append(conn, ks, s, [fact(2, "X", "Xavier", "diagnosis", "zqn8flu")])
    ev_read, ev_go, res = threading.Event(), threading.Event(), {}

    def dhook(name, data=None):
        if name == "derive_after_read" and not ev_read.is_set():
            res["derive_read_has_X"] = any(r[1] == "X" for r in data)
            ev_read.set(); ev_go.wait(30)
    p.hook = dhook

    def derive():
        try:
            res["derive"] = p.derive()
        except Exception as e:  # noqa: BLE001
            res["derive"] = type(e).__name__

    def inbox():
        try:
            log.inbox_enqueue(mk("n8-inbox"), K.KeyService(), s, "iX", "s1", "X", {"text": "zqn8inbox"})
            res["inbox"] = "accepted"
        except Exception as e:  # noqa: BLE001
            res["inbox"] = getattr(e, "sqlstate", type(e).__name__)

    def stale():
        r = log.append(mk("n8-stale"), K.KeyService(), L.Lease(t, s.scope, s.epoch - 1, "zombie", None), [msg(3, "zombie zqn8z", "X")])
        res["stale"] = r
    td = threading.Thread(target=derive); td.start(); ev_read.wait(30)
    ths = []

    def phook(name, data=None):
        if name == "phase1_before_commit":
            ev_go.set()
            for f in (inbox, stale):
                th = threading.Thread(target=f); th.start(); ths.append(th)
            waiting = 0
            for _ in range(60):
                waiting = CLOCK.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and application_name like 'n8-%%'").fetchone()[0]
                if waiting >= 3 or (all(not x.is_alive() for x in ths) and not td.is_alive()):
                    break
                threading.Event().wait(0.05)
            res["waiting_on_phase1"] = waiting
    rec = erasure.run(mk("n8-erase"), ks, es, erasure.request(conn, t, "X"), hook=phook)
    td.join(30); [x.join(30) for x in ths]
    p.hook = lambda *a: None
    p.run(); p.stop()
    sweep = erasure.sweep(conn, ks, es)
    resid = {"pg_facts_X": conn.execute("select count(*) from facts where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0],
             "pg_inbox_X": conn.execute("select count(*) from inbox where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0],
             "pg_quarantine_X": conn.execute("select count(*) from quarantine where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0],
             "es_X": es_count_all(es, t, Xq())}
    state = conn.execute("select state from erasure_jobs where tenant_id=%s", (t,)).fetchone()[0]
    holds = state == "done" and not any(resid.values())
    return holds, "" if holds else f"residue {resid} state {state}", {**res, "residue": resid, "job_state": state, "first_run_done": rec["done"], "sweep": sweep}


# ------------------------------------------------------------------ N4
class OpenJobOnlyReader(reader.Reader):
    """BROKEN reader (old semantics): hides tombstones only while an erasure job is open."""
    def _hidden(self, ids, subjects):
        if self.conn.execute("select 1 from erasure_jobs where tenant_id=%s and state <> 'done'", (self.t,)).fetchone():
            return super()._hidden(ids, subjects)
        return set(), set()


def n4(arm):
    dbname = CTL_DB if arm == "control" else None
    conn, ks, es = env(dbname)
    t = tenant("n4")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "hello zqn4y", "Y"), msg(1, "X says zqn4x", "X")])
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()

    def hook(name, data=None):
        if name == "after_fence":
            raise erasure.ESError(503, "injected_purge_failure")
    rec = erasure.erase(conn, ks, es, t, "X", hook=hook)
    residue = rec["verify"]["es:events"]
    state = conn.execute("select state from erasure_jobs where tenant_id=%s", (t,)).fetchone()[0]
    R = OpenJobOnlyReader if arm == "control" else reader.Reader
    served = len(R(conn, es, t).search("zqn4x"))
    holds = state != "done" and served == 0
    return holds, ("done with residue" if state == "done" else "") + (" reader served erased" if served else ""), {
        "residue_at_completion": residue, "job_state": state, "receipt_done": rec["done"], "reader_served": served, "error_type": rec["error_type"]}


# ------------------------------------------------------------------ N5
class UnfilteredReader(reader.Reader):
    """BROKEN reader: no tombstone filter (old query_facts)."""
    def _hidden(self, ids, subjects):
        return set(), set()


def n5(arm):
    conn, ks, es = env()
    t = tenant("n5")
    s = session(conn, t)
    log.append(conn, ks, s, [fact(0, "X", "Xavier", "diagnosis", "zqn5flu"), msg(1, "X says zqn5msg", "X"), fact(2, "Y", "Yolanda", "colour", "blue")])
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()

    def hook(name, data=None):
        if name == "after_phase1":
            raise RuntimeError("injected: stop after phase 1")
    try:
        erasure.erase(conn, ks, es, t, "X", hook=hook)
    except RuntimeError:
        pass
    state = conn.execute("select state from erasure_jobs where tenant_id=%s", (t,)).fetchone()[0]
    raw = es_count_live(es, t, "facts", Xq()) + es_count_live(es, t, "events", Xq())
    R = UnfilteredReader if arm == "control" else reader.Reader
    r = R(conn, es, t)
    got = len(r.facts("Xavier")) + len(r.facts("Xavier", "diagnosis", as_of="2026-03-02T00:00:00Z")) + len(r.search("zqn5msg"))
    erasure.sweep(conn, ks, es)
    return got == 0, ("erased fact/event served while pending" if got else ""), {"job_state_while_pending": state, "raw_es_docs_of_X": raw, "served": got}


# ------------------------------------------------------------------ N7
def n7(arm):
    dbname = CTL_DB if arm == "control" else None
    conn, ks, es = env(dbname)
    t = tenant("n7")
    s = session(conn, t)
    src = log.append(conn, ks, s, [msg(0, "please set scope", "U")])["accepted"][0]
    ts = msg(1, "", "U")["ts_event"]
    out = {}
    out["state_v0"] = log.decide(conn, ks, s, "U", "scope", 0, "state", {"text": "APAC"}, src, "k1", ts)[0]
    try:                                                        # (a) a read-back accepted against v0 after the head moved to 1
        out["a_stale_accept"] = log.decide(conn, ks, s, "U", "scope", 0, "accept_read_back", {"rb": "rb1"}, src, "k2", ts)[0]
    except log.StaleDecision:
        out["a_stale_accept"] = "refused"
    out["accept_v1"] = log.decide(conn, ks, s, "U", "scope", 1, "accept_read_back", {"rb": "rb2"}, src, "k3", ts)[0]
    out["revert_v2"] = log.decide(conn, ks, s, "U", "scope", 2, "revert", {"of": "k3"}, src, "k4", ts)[0]
    out["b_replay_same_key"] = log.decide(conn, ks, s, "U", "scope", 1, "accept_read_back", {"rb": "rb2"}, src, "k3", ts)[0]
    try:
        out["b_replay_new_key"] = log.decide(conn, ks, s, "U", "scope", 1, "accept_read_back", {"rb": "rb2"}, src, "k5", ts)[0]
    except log.StaleDecision:
        out["b_replay_new_key"] = "refused"
    # (c) lost response: two concurrent submissions of the same decision id, both past the idempotency pre-check
    hv = log.head(conn, ks, t, "s1", "scope")[0]
    bar, res = threading.Barrier(2, timeout=10), []

    def go():
        c = db.connect(db=dbname) if dbname else db.connect()
        try:
            res.append(_decide_with_barrier(c, K.KeyService(), s, hv, src, ts, bar))   # own connections per thread
        except log.StaleDecision:
            res.append("refused")
    th = [threading.Thread(target=go) for _ in range(2)]
    [x.start() for x in th]; [x.join() for x in th]
    out["c_concurrent_same_key"] = sorted(res)
    out["c_events_with_key"] = conn.execute("select count(*) from events where tenant_id=%s and idem_key='k6'", (t,)).fetchone()[0]
    out["head_final"] = log.head(conn, ks, t, "s1", "scope")[0]
    out["barrier_passed_by"] = 2 - bar.n_waiting if not bar.broken else "broken"
    holds = out["a_stale_accept"] == "refused" and out["b_replay_same_key"] == "duplicate" and out["b_replay_new_key"] == "refused" and out["c_events_with_key"] == 1
    why = [k for k, ok in (("a", out["a_stale_accept"] == "refused"), ("b", out["b_replay_new_key"] == "refused" and out["b_replay_same_key"] == "duplicate"),
                           ("c", out["c_events_with_key"] == 1)) if not ok]
    return holds, ("violated:" + ",".join(why)) if why else "", out


def _decide_with_barrier(c, ks, s, hv, src, ts, bar):
    import json as _j
    body = _j.dumps({"item": "scope", "action": "state", "payload": {"text": "EU"}, "based_on_version": hv}, sort_keys=True)
    r = log.append(c, ks, s, [dict(subject_id="U", kind="decision", actor="user", trust_origin="user_typed", ts_event=ts, content=body, idem_key="k6",
                                   decision={"item": "scope", "based_on_version": hv, "source_event_id": src})],
                   hook=lambda n, d=None: bar.wait() if n == "after_precheck" else None)
    return "accepted" if r["accepted"] else ("duplicate" if r["duplicates"] else "quarantined")


SCEN = {"N1": n1, "N2": n2, "N3": n3, "N3S_eraser": n3s_eraser, "N3S_projector": n3s_projector, "N4": n4, "N5": n5, "N7": n7, "N8": n8}


NO_CONTROL = {"N3S_eraser"}     # pre-registered: no sensitive control exists (the taker deletes the stale staging index)


def run(name, R):
    arms = {}
    for arm in (("fenced",) if name in NO_CONTROL else ("fenced", "control")):
        runs = []
        for i in range(R):
            try:
                holds, why, w = SCEN[name](arm)
            except Exception as e:  # noqa: BLE001
                holds, why, w = None, f"ERROR {type(e).__name__}: {str(e)[:160]}", {}
            runs.append({"holds": holds, "why": why, "witness": w})
        arms[arm] = {"holds": sum(1 for r in runs if r["holds"] is True), "violated": sum(1 for r in runs if r["holds"] is False),
                     "errors": sum(1 for r in runs if r["holds"] is None), "runs": runs}
    if name in NO_CONTROL:
        arms["control"] = {"holds": 0, "violated": 0, "errors": 0, "runs": [], "note": "no control (supporting witness only, not a PASS)"}
        ok = None if arms["fenced"]["holds"] == R else False
    else:
        ok = arms["fenced"]["holds"] == R and arms["control"]["violated"] >= int(0.9 * R)
    return {"pass": ok, "R": R, "fenced_holds": arms["fenced"]["holds"], "control_violated": arms["control"]["violated"],
            "errors": {a: arms[a]["errors"] for a in arms}, "arms": arms}


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    which = [a for a in sys.argv[1:] if not a.isdigit()] or list(SCEN)
    for n in which:
        res = run(n, R)
        print(n, {True: "PASS", False: "FAIL", None: "SUPPORTING(no control)"}[res["pass"]], "fenced", res["fenced_holds"], "/", R, "control violated", res["control_violated"], "/", R, "errors", res["errors"])
        print("  sample fenced witness:", json.dumps(res["arms"]["fenced"]["runs"][0], default=str)[:700])
        if res["arms"]["control"]["runs"]:
            print("  sample control witness:", json.dumps(res["arms"]["control"]["runs"][0], default=str)[:700])
        write_result(f"new_{n}", res)
