"""Reproduce the independent review's material findings (research/writepath-review-aaa0fec.md, F1-F7) as failing
properties on the aaa0fec write path. Run with the aaa0fec package first on the path:

  cd ~/emlab-16096/wp/aaa0fec_src && PYTHONPATH=. ../venv/bin/python ../src/old_repro/review_repro.py

Destructive probes (F3, F4) run in a scratch database built from aaa0fec's schema.sql (wp_scratch_review), never in
wp_events. Each case returns {"property": ..., "property_holds": bool, "witness": {...}}; a finding is reproduced when
property_holds is False. F7 cannot fail live on ES 9.5.6 (probes P3/P8 saw whole-request failure), so it is shown
against a stubbed response and labelled so."""
import json
import pathlib
import sys
import threading
import time
import uuid

import psycopg

from exp.common import write_result
from exp.util import tenant, msg, fact, session, es_count_all, es_count_live
from wp import db, erasure, families as F, keys as K, lease as L, log, projector
from wp.es import ES

HERE = pathlib.Path(__file__).resolve()
SCRATCH = "wp_scratch_review"
CODE = (pathlib.Path("wp") / "schema.sql").resolve()


def Xq(s="X"):
    return {"term": {"subject_id": s}}


def scratch():
    admin = db.connect(db="postgres")
    admin.execute(f'drop database if exists "{SCRATCH}"')
    admin.execute(f'create database "{SCRATCH}"')
    c = db.connect(db=SCRATCH)
    c.execute(CODE.read_text())
    c.execute("insert into store_registry values ('pg:events',true),('pg:facts',true),('es:events',true)")
    return c


# ---------------------------------------------------------------- F1
def f1():
    real = dict(F.SETTINGS)
    F.SETTINGS["index.max_terms_count"] = 2         # stands in for 65,537 ids (index.max_terms_count default 65,536)
    conn, ks, es = db.connect(), K.KeyService(), ES()
    t = tenant("rf1")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"X {i}", "X") for i in range(3)] + [msg(3, "Y", "Y")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    w = {}
    try:
        try:
            r = erasure.erase(conn, ks, es, t, "X")
            w["run"] = {"done": r["done"], "error_type": r["error_type"]}
        except Exception as e:  # noqa: BLE001
            w["run_raised"] = type(e).__name__
        st = conn.execute("select state from erasure_jobs where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0]
        lease_live = conn.execute("select expires_at > clock_timestamp(), holder from leases where tenant_id=%s and scope='projector'", (t,)).fetchone()
        w.update({"job_state": st, "projector_lease_still_held_by_eraser": bool(lease_live[0]) and lease_live[1].startswith("erasure:")})
        jy = erasure.request(conn, t, "Y")
        try:
            w["sweep"] = erasure.sweep(conn, ks, es)
        except Exception as e:  # noqa: BLE001
            w["sweep_raised"] = type(e).__name__
        w["Y_job_state"] = conn.execute("select state from erasure_jobs where job_id=%s", (jy,)).fetchone()[0]
    finally:
        F.SETTINGS.clear(); F.SETTINGS.update(real)
    holds = "run_raised" not in w and "sweep_raised" not in w and w["Y_job_state"] == "done"
    return {"property": "erasure verify cannot crash on a large subject; the lease is released; one failed job never blocks another",
            "property_holds": holds, "witness": w, "note": "max_terms_count patched to 2 in F.SETTINGS (rebuilt indices) to model > 65,536 ids"}


# ---------------------------------------------------------------- F2
def f2(attempts=5):
    conn, ks, es = db.connect(), K.KeyService(), ES()
    t = tenant("rf2")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X says zqrf2x", "X"), msg(1, "Z hello", "Z")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    stop = threading.Event()
    pstats = {"steps": 0, "starts": 0}

    def proj_loop():
        pc = projector.Projector(db.connect(), K.KeyService(), ES(), t, ttl_s=2)
        while not stop.is_set():
            if pc.lease is None:
                pstats["starts"] += 1
            pc.step(); pstats["steps"] += 1
            time.sleep(0.2)
        pc.stop()
    th = threading.Thread(target=proj_loop, daemon=True); th.start()

    def h(name, data=None):
        if name == "rebuild_before_swap:events":
            time.sleep(3)                                 # a rebuild that outlives the 2 s lease
    jid = erasure.request(conn, t, "X")
    dones = []
    for _ in range(attempts):
        try:
            dones.append(erasure.run(db.connect(), ks, es, jid, ttl_s=2, hook=h)["done"])
        except Exception as e:  # noqa: BLE001
            dones.append(f"raised {type(e).__name__}")
    stop.set(); th.join(10)
    x_live = es_count_live(es, t, "events", Xq())
    return {"property": "an erasure whose purge outlives the lease TTL still completes under concurrent projector load",
            "property_holds": any(d is True for d in dones) and x_live == 0,
            "witness": {"attempt_done": dones, "X_docs_live_after": x_live, "projector": pstats, "ttl_s": 2, "rebuild_stall_s": 3}}


# ---------------------------------------------------------------- F3 / F4 (scratch database)
def f3_f4():
    c = scratch()
    t = "rf3"
    out = {}
    role = c.execute("select rolsuper from pg_roles where rolname=current_user").fetchone()[0]
    owner = c.execute("select pg_get_userbyid(relowner) = current_user from pg_class where relname='facts'").fetchone()[0]
    c.execute("insert into leases values (%s,'projector',5,null,null)", (t,))
    c.execute("insert into subjects values (%s,'X',false)", (t,))

    def attempt(name, sql, args=()):
        try:
            with c.transaction():
                c.execute(sql, args)
            out[name] = "accepted"
        except psycopg.Error as e:
            out[name] = f"refused:{e.sqlstate}"
    # a stale projector (epoch 4, current 5) invents a scope and writes facts and another session's events through it
    attempt("junk_scope_lease", "insert into leases values (%s,'junk',1,null,null)", (t,))
    attempt("junk_scope_fact", "insert into facts(tenant_id,fact_id,subject_id,slot,body_enc,valid_from,input_event_ids,lease_scope,lease_epoch) "
                               "values (%s,'f1','X','s',''::bytea,now(),'{e1}','junk',1)", (t,))
    attempt("delete_facts", "delete from facts where tenant_id=%s", (t,))
    attempt("truncate_inbox", "truncate inbox")
    c.execute("update subjects set erased=true where tenant_id=%s", (t,))
    attempt("reset_erased", "update subjects set erased=false where tenant_id=%s", (t,))
    attempt("delete_tombstones", "delete from tombstones")
    attempt("delete_store_registry", "delete from store_registry where name='es:events'")
    attempt("disable_trigger", "alter table facts disable trigger facts_fence")
    attempt("session_replication_role", "set session_replication_role = replica")
    c.execute("reset session_replication_role")
    f3_holds = all(v.startswith("refused") for k, v in out.items())
    # F4: a fabricated completion with residue present
    c.execute("update subjects set erased=false where tenant_id=%s", (t,))
    c.execute("alter table facts enable trigger facts_fence")
    c.execute("insert into facts(tenant_id,fact_id,subject_id,slot,body_enc,valid_from,input_event_ids,lease_scope,lease_epoch) values (%s,'f2','X','s',''::bytea,now(),'{e2}','projector',5)", (t,))
    f4 = {}
    fake = json.dumps({"pg:events": 0, "pg:facts": 0})
    for name, sql, args in [
            ("insert_done_fabricated", "insert into erasure_jobs(job_id,tenant_id,subject_id,state,verify,lease_scope,lease_epoch) values ('j1',%s,'X','done',%s,'projector',5)", (t, fake)),
            ("update_done_fabricated", "update erasure_jobs set state='done', verify=%s where job_id='j2'", (fake,))]:
        if name.startswith("update"):
            c.execute("insert into erasure_jobs(job_id,tenant_id,subject_id,lease_scope,lease_epoch) values ('j2',%s,'X','projector',5)", (t,))
        try:
            with c.transaction():
                c.execute(sql, args)
            f4[name] = "accepted"
        except psycopg.Error as e:
            f4[name] = f"refused:{e.sqlstate}"
    residue = c.execute("select count(*) from facts where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0]
    c.close()
    db.connect(db="postgres").execute(f'drop database "{SCRATCH}"')
    return ({"property": "a stale or dishonest app-role caller cannot bypass the fence or the guards", "property_holds": f3_holds,
             "witness": dict(out, role_is_superuser=role, role_owns_tables=owner)},
            {"property": "the store refuses 'done' while Postgres residue exists, whatever verify JSON the caller supplies",
             "property_holds": all(v.startswith("refused") for v in f4.values()), "witness": dict(f4, facts_residue_for_X=residue)})


# ---------------------------------------------------------------- F5 decisions
def f5():
    conn, ks = db.connect(), K.KeyService()
    w = {}
    ts = msg(1, "", "U")["ts_event"]
    # (a) executor reads the head immediately before executing (natural code); the approval was amended meanwhile
    t = tenant("rf5a"); s = session(conn, t)
    src = log.append(conn, ks, s, [msg(0, "approve deploy v3", "U")])["accepted"][0]
    log.decide(conn, ks, s, "U", "deploy", 0, "approve", {"v": 3}, src, "ap", ts)
    log.decide(conn, ks, s, "U", "deploy", 1, "amend", {"v": 4}, src, "am", ts)
    v = log.head(conn, ks, t, "s1", "deploy")[0]
    try:
        w["a_execute_after_amend_reading_head"] = log.decide(conn, ks, s, "U", "deploy", v, "execute", {"approval": "ap"}, src, "ex", ts)[0]
    except log.StaleDecision:
        w["a_execute_after_amend_reading_head"] = "refused"
    # (b) amend in another session does not move this session's head
    t = tenant("rf5b"); s1 = session(conn, t, "s1"); s2 = session(conn, t, "s2")
    src1 = log.append(conn, ks, s1, [msg(0, "approve", "U")])["accepted"][0]
    src2 = log.append(conn, ks, s2, [msg(0, "amend", "U")])["accepted"][0]
    log.decide(conn, ks, s1, "U", "deploy", 0, "approve", {}, src1, "b1", ts)
    log.decide(conn, ks, s2, "U", "deploy", 0, "amend", {}, src2, "b2", ts)
    try:
        w["b_execute_after_cross_session_amend"] = log.decide(conn, ks, s1, "U", "deploy", 1, "execute", {}, src1, "b3", ts)[0]
    except log.StaleDecision:
        w["b_execute_after_cross_session_amend"] = "refused"
    # (c) one typed message authorises several decisions; a decision event is accepted as a source
    t = tenant("rf5c"); s = session(conn, t)
    src = log.append(conn, ks, s, [msg(0, "ok", "U")])["accepted"][0]
    many = [log.decide(conn, ks, s, "U", f"item{i}", 0, "approve", {}, src, f"c{i}", ts)[0] for i in range(3)]
    w["c_decisions_from_one_source"] = many
    d_id = log.decide(conn, ks, s, "U", "itemZ", 0, "approve", {}, src, "cz", ts)[1]
    try:
        w["c_decision_as_source"] = log.decide(conn, ks, s, "U", "itemW", 0, "approve", {}, d_id, "cw", ts)[0] if d_id else "n/a"
    except log.StaleDecision:
        w["c_decision_as_source"] = "refused"
    except psycopg.Error as e:
        w["c_decision_as_source"] = f"refused:{e.sqlstate}"
    # (d) idempotency key reused with different content; key collision across sessions
    t = tenant("rf5d"); s1 = session(conn, t, "s1"); s2 = session(conn, t, "s2")
    r1 = log.append(conn, ks, s1, [msg(0, "content A", "U", idem_key="k1")])
    try:
        r2 = log.append(conn, ks, s1, [msg(1, "content B (different)", "U", idem_key="k1")])
        w["d_same_key_different_content"] = "duplicate" if r2["duplicates"] else "accepted"
    except psycopg.Error as e:
        w["d_same_key_different_content"] = f"refused:{e.sqlstate}"
    r3 = log.append(conn, ks, s2, [msg(0, "s2 own event", "U", idem_key="k1")])
    w["d_other_session_same_key"] = "got s1's event as duplicate" if r3["duplicates"] == r1["accepted"] else ("accepted" if r3["accepted"] else str(r3))
    holds = (w["a_execute_after_amend_reading_head"] == "refused" and w["b_execute_after_cross_session_amend"] == "refused"
             and w["c_decisions_from_one_source"].count("accepted") <= 1 and w["c_decision_as_source"] != "accepted"
             and w["d_same_key_different_content"].startswith("refused") and w["d_other_session_same_key"] == "accepted")
    return {"property": "the store binds execution to the approval, heads are per item, one source authorises one decision, "
                        "decisions are not sources, an idempotency key never silently maps different content", "property_holds": holds, "witness": w}


# ---------------------------------------------------------------- F6 liveness
def f6():
    w = {}
    conn, ks = db.connect(), K.KeyService()
    # (a) a zombie stalls after its insert (holding FOR SHARE on its lease row); takeover with no lock timeout
    t = tenant("rf6"); a = session(conn, t)
    stall = 8.0
    res = {}

    def zombie():
        def hk(name, data=None):
            if name == "before_commit":
                time.sleep(stall)
        res["zombie"] = log.append(db.connect(), K.KeyService(), a, [msg(0, "zombie", "U")], hook=hk)

    def taker():
        t0 = time.time()
        b = L.acquire(db.connect(), t, L.session_scope("s1"), 30, force=True)     # lock_timeout_ms=None (default)
        res["takeover_s"] = round(time.time() - t0, 2); res["b_epoch"] = b.epoch
    z = threading.Thread(target=zombie); z.start()
    time.sleep(1.0)
    k = threading.Thread(target=taker); k.start()
    k.join(30); z.join(30)
    zep = conn.execute("select lease_epoch from events where tenant_id=%s", (t,)).fetchall()
    w["a"] = {"zombie_stall_s": stall, "takeover_wait_s": res.get("takeover_s"), "zombie_events_committed_epochs": [r[0] for r in zep]}
    # (b) quarantine is unbounded
    t = tenant("rf6q"); s = session(conn, t)
    stale = L.Lease(t, s.scope, s.epoch - 1, "z", None)
    for i in range(10):
        log.append(conn, ks, stale, [msg(j, "spam", "U") for j in range(50)])
    w["b_quarantine_rows_from_one_zombie"] = conn.execute("select count(*) from quarantine where tenant_id=%s", (t,)).fetchone()[0]
    # (c) cluster-wide xmin stall: a writing transaction open in ANOTHER database stalls this tenant's projection
    admin = db.connect(db="postgres")
    admin.execute("drop database if exists wp_scratch_xmin"); admin.execute("create database wp_scratch_xmin")
    other = db.connect(db="wp_scratch_xmin", autocommit=False)
    other.execute("create table t(x int)"); other.commit()
    other.execute("insert into t values (1)")          # holds an XID, stays open
    t = tenant("rf6x"); s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"after {i}", "U") for i in range(5)])
    es = ES()
    p = projector.Projector(conn, ks, es, t); p.run(max_rounds=5)
    during = es_count_all(es, t, {"match_all": {}})
    other.rollback(); other.close()
    p.run(); p.stop()
    after = es_count_all(es, t, {"match_all": {}})
    admin.execute("drop database wp_scratch_xmin")
    w["c"] = {"projected_while_other_db_txn_open": during, "projected_after_it_ended": after, "appended": 5}
    holds = (res.get("takeover_s") or 99) < 3 and w["b_quarantine_rows_from_one_zombie"] <= 100 and during == 5
    return {"property": "takeover completes within a bound; quarantine is bounded; another database's open transaction does not stall projection",
            "property_holds": holds, "witness": w}


# ---------------------------------------------------------------- F7
def f7():
    class Stub(ES):
        def req(self, method, path, body=None, ok=(200,), **kw):
            return 200, {"acknowledged": True, "errors": True,
                         "action_results": [{"action": {"type": "remove", "indices": ["a"], "aliases": ["x"]}, "status": 404, "error": {"type": "aliases_not_found_exception"}}]}
    try:
        Stub().aliases([{"remove": {"index": "a", "alias": "x"}}])
        r = "returned (treated as success)"
    except Exception as e:  # noqa: BLE001
        r = f"raised {type(e).__name__}"
    return {"property": "an alias response with errors:true is an error", "property_holds": r.startswith("raised"),
            "witness": {"stubbed_response": True, "outcome": r}}


if __name__ == "__main__":
    out = {"code_under_test": "aaa0fec (writepath/wp archived to WP_HOME/aaa0fec_src)"}
    for name, fn in [("F1", f1), ("F2", f2), ("F5", f5), ("F6", f6), ("F7", f7)]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        print(name, json.dumps(out[name], default=str)[:600], flush=True)
    try:
        out["F3"], out["F4"] = f3_f4()
    except Exception as e:  # noqa: BLE001
        out["F3"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    print("F3", json.dumps(out["F3"], default=str)[:600]); print("F4", json.dumps(out.get("F4"), default=str)[:600])
    write_result("review_repro_aaa0fec", out)
