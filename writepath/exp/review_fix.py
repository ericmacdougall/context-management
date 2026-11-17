"""Review findings F1, F3-F7 on the NEW write path. Every property has a control that is the shipped system with ONE
named patch (exp/patches.py); the patch text is recorded. Thresholds: exp/THRESHOLDS.md amendment 3.
usage: python -m exp.review_fix [F1 F1S F3 F4 F5 F6 F7] [--n100k N]"""
import json
import sys
import threading
import time

import psycopg

from exp.common import write_result
from exp.patches import patched, variant, NOOP, drop_duplicate_decisions
from exp.util import env, tenant, msg, fact, session, es_count_all
from wp import config, db, erasure, keys as K, lease as L, log, projector, registry
from wp.es import ES
from wp.verifier import Verifier


def _try(conn, sql, args=()):
    """Run an attack inside a transaction that is always rolled back (the store is unchanged either way) and report
    the rows it affected: an attack that touches 0 rows is 'accepted:0', which counts as neither a refusal nor a breach."""
    try:
        with conn.transaction(force_rollback=True):
            cur = conn.execute(sql, args)
            return f"accepted:{cur.rowcount}"
    except psycopg.Error as e:
        return f"refused:{e.sqlstate}"


def _breach(outcome):
    return outcome.startswith("accepted") and outcome != "accepted:0"


# ---------------------------------------------------------------- F1: one subject with N events (default 100,000)
def f1_large(n):
    conn, ks, es = env()
    t = tenant("f1big")
    s = session(conn, t, ttl=600)
    t0 = time.time()
    B = 1000
    for i in range(0, n, B):
        evs = [msg(i + j, f"X event {i + j}", "X") if (i + j) % 1000 else fact(i + j, "X", "Xavier", f"a{i + j}", f"v{i + j}") for j in range(min(B, n - i))]
        r = log.append(conn, ks, s, evs)
        assert len(r["accepted"]) == len(evs), r
    log.append(conn, ks, s, [msg(n + 1, "Y survives zqf1y", "Y"), fact(n + 2, "Y", "Yolanda", "colour", "blue")])
    t_append = round(time.time() - t0, 1)
    t0 = time.time()
    p = projector.Projector(conn, ks, es, t, ttl_s=120, batch=2000)
    p.run(max_rounds=10000); p.stop()
    t_project = round(time.time() - t0, 1)
    x_before = es_count_all(es, t, {"term": {"subject_id": "X"}})
    rec = erasure.erase(conn, ks, es, t, "X", ttl_s=120, physical="wait")
    x_after = es_count_all(es, t, {"term": {"subject_id": "X"}})
    y_after = es_count_all(es, t, {"term": {"subject_id": "Y"}})
    holds = rec["done"] and x_after == 0 and y_after == 3 and x_before > 65536     # Y: 2 event docs + 1 fact doc (amendment 4)
    return {"property": "erasure of one subject with N events completes (done) with zero residue in every store",
            "N_events": n, "property_holds": holds,
            "witness": {"X_docs_before": x_before, "X_docs_after": x_after, "Y_docs_after": y_after, "append_s": t_append, "project_s": t_project,
                        "receipt": {k: rec.get(k) for k in ("done", "error_type", "verify", "steps", "seconds")}},
            "cascade_linearity": _linearity((rec.get("steps") or {}).get("cascade_batch_s") or []),
            "control": "aaa0fec at the same property: review_repro F1 (verify ESError, job stuck, lease held, sweep blocked)"}


def _linearity(per):
    """D5 (THRESHOLDS amendment 5): the last 5 full batches take <= 2x the first 5 (the 963e597 cascade grew per batch)."""
    full = per[:-2] if len(per) > 12 else per          # the last two calls are the empty-cursor and finalize passes
    if len(full) < 10:
        return {"batches": len(per), "verdict": "too few batches"}
    a, b = sum(full[:5]), sum(full[-5:])
    return {"batches": len(per), "first5_s": round(a, 3), "last5_s": round(b, 3), "linear": b <= 2 * a}


# ---------------------------------------------------------------- F1: sweep isolation
def f1_sweep(arm):
    conn, ks, es = env()
    t = tenant("f1sw")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "A", "A"), msg(1, "B", "B")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    ja, jb = erasure.request(conn, t, "A"), erasure.request(conn, t, "B")
    calls = {"n": 0}

    def boom(name, data=None):
        if name == "after_mark" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("injected failure in the first job")
    if arm == "fenced":
        out = erasure.sweep(conn, ks, es, tenant=t, hook=boom, physical="wait")
    else:                                                  # PATCH: the aaa0fec sweep (one failure aborts the sweep)
        out = {}
        try:
            for (jid,) in conn.execute("select job_id from erasure_jobs where tenant_id=%s and state <> 'done' order by created_at", (t,)).fetchall():
                r = erasure.run(conn, ks, es, jid, hook=boom, physical="wait")
                if r["error_type"]:
                    raise RuntimeError(r["error_type"])
                out[jid] = r["done"]
        except RuntimeError as e:
            out["aborted"] = str(e)
    st = dict(conn.execute("select job_id, state from erasure_jobs where tenant_id=%s", (t,)).fetchall())
    a_released = conn.execute("select claim_expires < clock_timestamp() from erasure_jobs where job_id=%s", (ja,)).fetchone()[0]
    holds = st[jb] == "done" and st[ja] != "done" and a_released
    return holds, ("" if holds else f"B={st[jb]} A_released={a_released}"), {"states": {"A": st[ja], "B": st[jb]}, "A_claim_released": a_released,
                                                                             "injected_failures": calls["n"], "sweep": {k[:6]: v for k, v in out.items()}}


# ---------------------------------------------------------------- F3: attacks by the app role
def _prep(conn, ks, es):
    t = tenant("f3")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X hi", "X"), fact(1, "X", "Xavier", "k", "v")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    return t, s


def _attack_fact(conn, t, scope, epoch):
    return _try(conn, "insert into facts(tenant_id,fact_id,subject_id,slot,body_enc,valid_from,input_event_ids,lease_scope,lease_epoch) "
                      "values (%s,%s,'X','s',''::bytea,now(),'{e}',%s,%s)", (t, "fz" + str(time.time_ns()), scope, epoch))


def _own(tbl, trig, priv):
    return ([f"grant {priv} on {tbl} to wp_app", f"alter table {tbl} disable trigger {trig}"],
            [f"revoke {priv} on {tbl} from wp_app", f"alter table {tbl} enable always trigger {trig}"])


# amendment 18a: every verifiable store wp_erasure_complete requires (not kind pg), from the registry, so a store added later
# (keys:physical_pending, 71b3ecc) cannot turn a correct completion into a done_missing_stores refusal
ZERO_COUNTS = {st.name: 0 for st in registry.STORES if st.verifiable and st.kind != "pg"}
ZEROS = json.dumps(ZERO_COUNTS)


def _tombstoned(c, t):
    """Give the tenant real tombstones (mark + cascade of X through the app's own functions) so the attack is not vacuous."""
    jid = erasure.request(c, t, "X")
    c.execute("select wp_erasure_mark(%s)", (jid,))
    while c.execute("select wp_erasure_cascade(%s, 1000)", (jid,)).fetchone()[0]:
        pass
    return t


def _ready(c, t):
    jid = erasure.request(c, t, "Z")                       # a subject with no data: completion is otherwise valid
    c.execute("update erasure_jobs set state='keys_destroyed' where job_id=%s", (jid,))
    return jid


F3_ATTACKS = {
    # name: (attack(conn, t, s) -> outcome, (patch apply, revert), patch label)
    "junk_scope_fact": (lambda c, t, s: (lambda jl: _attack_fact(c, t, jl.scope, jl.epoch))(L.acquire(c, t, "session:junk")),
                        variant("wp_fence_trg", "if NEW.lease_scope is distinct from want then", "if false then"), "scope binding removed from wp_fence_trg"),
    "lease_direct_insert": (lambda c, t, s: _try(c, "insert into leases values (%s,'session:zz',99,null,null)", (t,)),
                            _own("leases", "leases_owner_only", "insert"), "app INSERT on leases (privilege + owner-only trigger)"),
    "delete_facts": (lambda c, t, s: _try(c, "delete from facts where tenant_id=%s", (t,)), _own("facts", "facts_delete_owner_only", "delete"),
                     "app DELETE on facts (privilege + owner-only trigger)"),
    "truncate_inbox": (lambda c, t, s: _try(c, "truncate inbox"), _own("inbox", "inbox_no_truncate", "truncate"), "app TRUNCATE on inbox (privilege + trigger)"),
    "reset_erased": (lambda c, t, s: _try(c, "update subjects set erased=false where tenant_id=%s", (t,)), _own("subjects", "subjects_owner_only", "update"),
                     "app UPDATE on subjects"),
    "delete_tombstones": (lambda c, t, s: _try(c, "delete from tombstones where tenant_id=%s", (_tombstoned(c, t),)), _own("tombstones", "tombstones_owner_only", "delete"),
                          "app DELETE on tombstones"),
    "delete_store_registry": (lambda c, t, s: _try(c, "delete from store_registry where name='dir:logs'"), _own("store_registry", "store_registry_owner_only", "delete"),
                              "app DELETE on store_registry"),
    "chains_tamper": (lambda c, t, s: _try(c, "update chains set next_seq=next_seq where tenant_id=%s", (t,)), _own("chains", "chains_owner_only", "update"),
                      "app UPDATE on chains"),
    "disable_trigger": (lambda c, t, s: _try(c, "alter table facts disable trigger facts_fence"),
                        (["grant wp_owner to wp_app"], ["alter table facts enable always trigger facts_fence", "revoke wp_owner from wp_app"]),
                        "wp_app made a member of the owner role"),
    "set_replication_role": (lambda c, t, s: _try(c, "set session_replication_role = replica"),
                             (["grant set on parameter session_replication_role to wp_app"], ["revoke set on parameter session_replication_role from wp_app"]),
                             "SET session_replication_role granted to wp_app"),
    "app_sets_done": (lambda c, t, s: _try(c, "update erasure_jobs set state='done' where job_id=%s", (erasure.request(c, t, "X"),)),
                      variant("erasure_guard", [("if (NEW.state = 'done' or NEW.verify is distinct from OLD.verify) and current_user <> 'wp_owner' then", "if false then"),
                                               ("if NEW.state <> OLD.state and NEW.state in ('marked', 'cascaded', 'logically_erased', 'done') and current_user <> 'wp_owner' then",
                                                "if false then")]),
                      "both owner checks removed from erasure_guard (done/verify, store-written states; amendment 18a)"),
    "app_inserts_done": (lambda c, t, s: _try(c, "insert into erasure_jobs(job_id,tenant_id,subject_id,state) values (%s,%s,'X','done')", ("jd" + str(time.time_ns()), t)),
                         variant("erasure_guard", "if TG_OP = 'INSERT' and (NEW.state <> 'requested' or NEW.verify is not null) then", "if false then"),
                         "insert-state check removed from erasure_guard"),
    "app_calls_complete": (lambda c, t, s: _try(c, "select wp_erasure_complete(%s, %s::jsonb, 'x')", (_ready(c, t), ZEROS)),
                           (["grant execute on function wp_erasure_complete(text, jsonb, text) to wp_app"],
                            ["revoke execute on function wp_erasure_complete(text, jsonb, text) from wp_app"]), "EXECUTE on wp_erasure_complete granted to wp_app"),
}


def _replica_stale_fact(arm):
    """Under session_replication_role=replica (granted in BOTH arms), a stale projector fact must still be refused:
    the guard triggers are ENABLE ALWAYS. Control: facts_fence switched to ORIGIN mode (one trigger, one flag)."""
    conn, ks, es = env()
    t, s = _prep(conn, ks, es)
    pl = L.acquire(conn, t, L.PROJECTOR, force=True)
    L.acquire(conn, t, L.PROJECTOR, force=True)            # pl is now stale
    base = (["grant set on parameter session_replication_role to wp_app"], ["revoke set on parameter session_replication_role from wp_app"])
    extra = (["alter table facts enable trigger facts_fence"], ["alter table facts enable always trigger facts_fence"]) if arm == "control" else NOOP
    with patched(base[0] + extra[0], extra[1] + base[1]):
        c = db.connect()
        c.execute("set session_replication_role = replica")
        r = _attack_fact(c, t, "projector", pl.epoch)
        c.close()
    return r


def f3():
    conn, ks, es = env()
    out = {}
    for name, (atk, (ap, rv), label) in F3_ATTACKS.items():
        t, s = _prep(conn, ks, es)
        fenced = atk(db.connect(), t, s)
        t2, s2 = _prep(conn, ks, es)
        with patched(ap, rv):
            control = atk(db.connect(), t2, s2)
        out[name] = {"fenced": fenced, "control": control, "patch": label}
    out["replica_mode_stale_fact"] = {"fenced": _replica_stale_fact("fenced"), "control": _replica_stale_fact("control"),
                                      "patch": "facts_fence ENABLE (origin) instead of ENABLE ALWAYS; SET session_replication_role granted in both arms"}
    holds = all(v["fenced"].startswith("refused") for v in out.values())
    ctl = sum(1 for v in out.values() if _breach(v["control"]))
    return {"property": "the app role cannot bypass the fence or the guards", "pass": holds and ctl == len(out),
            "fenced_refused": sum(1 for v in out.values() if v["fenced"].startswith("refused")), "attacks": len(out), "controls_accepted": ctl, "cases": out}


# ---------------------------------------------------------------- F4: completion is recomputed by the store
def _ready_job_with_pg_residue(conn, ks, es):
    """A job pushed to keys_destroyed WITHOUT doing the work (the app may set intermediate states): facts residue remains."""
    t = tenant("f4")
    s = session(conn, t)
    log.append(conn, ks, s, [fact(0, "X", "Xavier", "k", "v")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    jid = erasure.request(conn, t, "X")
    conn.execute("update erasure_jobs set state='keys_destroyed' where job_id=%s", (jid,))
    return t, jid


def f4():
    conn, ks, es = env()
    zeros = dict(ZERO_COUNTS)                              # amendment 18a
    out = {}
    # (a) fabricated zeros from the verifier role while Postgres residue exists
    ap, rv = variant("wp_erasure_complete", "v := wp_erasure_pg_residue(j.tenant_id, j.subject_id) || coalesce(ext, '{}'::jsonb);", "v := coalesce(ext, '{}'::jsonb);")
    res = {}
    for arm in ("fenced", "control"):
        t, jid = _ready_job_with_pg_residue(conn, ks, es)
        with patched(ap, rv) if arm == "control" else patched(*NOOP):
            r = db.verifier().execute("select wp_erasure_complete(%s, %s, 'x')", (jid, json.dumps(zeros))).fetchone()[0]
        res[arm] = {"done": r["done"], "residue": r.get("residue")}
    out["fabricated_zeros_with_pg_residue"] = dict(res, patch="wp_erasure_complete takes verify from the caller only")
    # (b) a registered verifiable store missing from the verifier's counts
    res = {}
    for arm in ("fenced", "control"):
        t, jid = _ready_job_with_pg_residue(conn, ks, es)
        conn.execute("select wp_erasure_mark(%s)", (jid,))
        while conn.execute("select wp_erasure_cascade(%s, 1000)", (jid,)).fetchone()[0]:
            pass
        conn.execute("update erasure_jobs set state='keys_destroyed' where job_id=%s", (jid,))
        partial = {k: v for k, v in zeros.items() if k != "es:facts"}
        patch = (["set role wp_owner", "delete from store_registry where name='es:facts'", "reset role"],
                 ["set role wp_owner", "insert into store_registry values ('es:facts','es',true) on conflict do nothing", "reset role"])
        with patched(*patch) if arm == "control" else patched(*NOOP):
            try:
                r = db.verifier().execute("select wp_erasure_complete(%s, %s, 'x')", (jid, json.dumps(partial))).fetchone()[0]
                res[arm] = {"done": r["done"]}
            except psycopg.Error as e:
                res[arm] = {"refused": e.sqlstate}
    out["missing_store_count"] = dict(res, patch="store_registry row es:facts removed")
    # (c) unregistered-store gate: a rogue table blocks completion
    res = {}
    for arm in ("fenced", "control"):
        t = tenant("f4g"); s = session(conn, t)
        log.append(conn, ks, s, [msg(0, "X", "X")])
        p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
        v = Verifier()
        if arm == "control":
            orig = v.ext_counts

            def no_gate(t_, s_, expunge=True, wait_s=180.0, _o=orig):
                o, i = _o(t_, s_, expunge, wait_s)
                o["gate:unregistered"] = 0
                return o, i
            v.ext_counts = no_gate
        with patched(["set role wp_owner", "create table zz_rogue_cache(x text)", "reset role"], ["drop table zz_rogue_cache"]):
            rec = erasure.erase(conn, ks, es, t, "X", verifier=v, physical="wait")
        res[arm] = {"done": rec["done"], "gate": (rec.get("verify") or {}).get("gate:unregistered")}
    out["unregistered_gate"] = dict(res, patch="verifier reports gate:unregistered=0")
    fen = [out["fabricated_zeros_with_pg_residue"]["fenced"]["done"] is False, "refused" in out["missing_store_count"]["fenced"],
           out["unregistered_gate"]["fenced"]["done"] is False]
    ctl = [out["fabricated_zeros_with_pg_residue"]["control"]["done"] is True, out["missing_store_count"]["control"].get("done") is True,
           out["unregistered_gate"]["control"]["done"] is True]
    return {"property": "'done' requires store-recomputed Postgres residue 0, every registered store counted, and no unregistered store",
            "pass": all(fen) and all(ctl), "fenced_holds": sum(fen), "controls_violated": sum(ctl), "cases": out}


# ---------------------------------------------------------------- F5: decisions bound by the store
def _dec_setup(conn, ks, sessions=("s1",)):
    t = tenant("f5")
    ss = {sid: session(conn, t, sid) for sid in sessions}
    return t, ss


def _src(conn, ks, s, text, i=0):
    return log.append(conn, ks, s, [msg(i, text, "U")])["accepted"][0]


def _d(conn, ks, s, item, action, based_on, src, key):
    try:
        return log.decide(conn, ks, s, "U", item, action, based_on, {"a": action}, src, key, msg(9, "", "U")["ts_event"])[0]
    except log.StaleDecision as e:
        return "refused:" + str(e)[:5]
    except log.IdempotencyConflict:
        return "refused:idem"


def f5_cases(conn, ks):
    out = {}
    # a1: approve, amend, executor reads the head and executes against it
    t, ss = _dec_setup(conn, ks); s = ss["s1"]
    ap = log.decide(conn, ks, s, "U", "deploy", "approve", None, {}, _src(conn, ks, s, "approve v3"), "k1", msg(1, "", "U")["ts_event"])[1]
    am = log.decide(conn, ks, s, "U", "deploy", "amend", ap, {}, _src(conn, ks, s, "amend v4", 1), "k2", msg(2, "", "U")["ts_event"])[1]
    hv = log.head(conn, ks, t, "deploy")[1]
    out["a1_execute_reading_head_after_amend"] = _d(conn, ks, s, "deploy", "execute", hv, _src(conn, ks, s, "go", 2), "k3")
    # a2: approve v1, re-approve v2; executing against v1 is caught ONLY by the head compare-and-set
    t, ss = _dec_setup(conn, ks); s = ss["s1"]
    v1 = log.decide(conn, ks, s, "U", "deploy", "approve", None, {}, _src(conn, ks, s, "approve v1"), "a1", msg(1, "", "U")["ts_event"])[1]
    log.decide(conn, ks, s, "U", "deploy", "approve", v1, {}, _src(conn, ks, s, "approve v2", 1), "a2", msg(2, "", "U")["ts_event"])
    out["a2_execute_citing_superseded_approval"] = _d(conn, ks, s, "deploy", "execute", v1, _src(conn, ks, s, "go", 2), "a3")
    # b1: re-approval in ANOTHER session moves the tenant-wide head (per-session heads would accept); caught only by the CAS
    t, ss = _dec_setup(conn, ks, ("s1", "s2"))
    ap = log.decide(conn, ks, ss["s1"], "U", "deploy", "approve", None, {}, _src(conn, ks, ss["s1"], "approve"), "b1", msg(1, "", "U")["ts_event"])[1]
    log.decide(conn, ks, ss["s2"], "U", "deploy", "approve", ap, {}, _src(conn, ks, ss["s2"], "approve v2"), "b2", msg(2, "", "U")["ts_event"])
    out["b1_execute_after_cross_session_reapproval"] = _d(conn, ks, ss["s1"], "deploy", "execute", ap, _src(conn, ks, ss["s1"], "go", 1), "b3")
    # b2: amend in another session (caught by the CAS and by the approve-required check: control removes both)
    t, ss = _dec_setup(conn, ks, ("s1", "s2"))
    ap = log.decide(conn, ks, ss["s1"], "U", "deploy", "approve", None, {}, _src(conn, ks, ss["s1"], "approve"), "c1", msg(1, "", "U")["ts_event"])[1]
    log.decide(conn, ks, ss["s2"], "U", "deploy", "amend", ap, {}, _src(conn, ks, ss["s2"], "amend"), "c2", msg(2, "", "U")["ts_event"])
    out["b2_execute_after_cross_session_amend"] = _d(conn, ks, ss["s1"], "deploy", "execute", ap, _src(conn, ks, ss["s1"], "go", 1), "c3")
    # c1: one typed message authorises one decision
    t, ss = _dec_setup(conn, ks); s = ss["s1"]
    src = _src(conn, ks, s, "ok")
    first = _d(conn, ks, s, "i1", "approve", None, src, "c1")
    out["c1_second_decision_same_source"] = _d(conn, ks, s, "i2", "approve", None, src, "c2")
    # c2: a decision cannot be a source
    did = log.head(conn, ks, t, "i1")[1]
    out["c2_decision_as_source"] = _d(conn, ks, s, "i3", "approve", None, did, "c3")
    # d1: same idempotency key, different request
    r1 = log.append(conn, ks, s, [msg(5, "content A", "U", idem_key="kk")])
    try:
        log.append(conn, ks, s, [msg(6, "content B", "U", idem_key="kk")])
        out["d1_same_key_different_content"] = "accepted_or_duplicate"
    except log.IdempotencyConflict:
        out["d1_same_key_different_content"] = "refused:idem"
    # positive controls of the mechanism (must be accepted)
    t, ss = _dec_setup(conn, ks); s = ss["s1"]
    ap = log.decide(conn, ks, s, "U", "deploy", "approve", None, {}, _src(conn, ks, s, "approve"), "p1", msg(1, "", "U")["ts_event"])[1]
    out["pos_execute_current_approval"] = _d(conn, ks, s, "deploy", "execute", ap, _src(conn, ks, s, "go", 1), "p2")
    out["pos_first_decision"] = first
    t, ss = _dec_setup(conn, ks, ("s1", "s2"))
    a = log.append(conn, ks, ss["s1"], [msg(0, "s1 own", "U", idem_key="same")])["accepted"]
    b = log.append(conn, ks, ss["s2"], [msg(0, "s2 own", "U", idem_key="same")])["accepted"]
    out["pos_same_key_other_session_is_own_event"] = "accepted" if a and b and a != b else "wrong"
    return out


F5_EXPECT_REFUSED = ["a1_execute_reading_head_after_amend", "a2_execute_citing_superseded_approval", "b1_execute_after_cross_session_reapproval",
                     "b2_execute_after_cross_session_amend",
                     "c1_second_decision_same_source", "c2_decision_as_source", "d1_same_key_different_content"]
F5_CONTROLS = {
    "a1_execute_reading_head_after_amend": variant("events_guard", "if NEW.decision_action = 'execute' and hd.last_action is distinct from 'approve' then",
                                                   "if false then"),
    "a2_execute_citing_superseded_approval": variant("events_guard", "if hd.last_event_id is distinct from NEW.based_on_event_id then", "if false then"),
    "b1_execute_after_cross_session_reapproval": variant("events_guard", "if hd.last_event_id is distinct from NEW.based_on_event_id then", "if false then"),
    "b2_execute_after_cross_session_amend": variant("events_guard", [("if hd.last_event_id is distinct from NEW.based_on_event_id then", "if false then"), ("if NEW.decision_action = 'execute' and hd.last_action is distinct from 'approve' then", "if false then")]),
    "c1_second_decision_same_source": tuple(a + b for a, b in zip(
        variant("events_guard", "if exists (select 1 from events where tenant_id = NEW.tenant_id and kind = 'decision' and source_event_id = NEW.source_event_id) then",
                "if false then"),
        (["drop index decision_source_once"], [drop_duplicate_decisions, "set role wp_owner", "create unique index decision_source_once on events (tenant_id, source_event_id) where kind = 'decision'", "reset role"]))),
    "c2_decision_as_source": variant("events_guard", "src.kind = 'decision' or ", ""),
}


def f5():
    conn, ks = db.connect(), K.KeyService()
    fenced = f5_cases(conn, ks)
    controls = {}
    for case, (ap, rv) in F5_CONTROLS.items():
        with patched(ap, rv):
            controls[case] = f5_cases(conn, ks)[case]
    real = K.req_hmac                                        # d1 control: no request fingerprint (the aaa0fec behaviour)
    K.req_hmac = lambda key, canonical: "same"
    try:
        controls["d1_same_key_different_content"] = f5_cases(conn, ks)["d1_same_key_different_content"]
    finally:
        K.req_hmac = real
    f_ok = [fenced[k].startswith("refused") for k in F5_EXPECT_REFUSED] + [fenced[k] == "accepted" for k in
                                                                          ("pos_execute_current_approval", "pos_first_decision", "pos_same_key_other_session_is_own_event")]
    c_viol = [not controls[k].startswith("refused") for k in F5_EXPECT_REFUSED]
    return {"property": "the store binds decisions to the approval, the tenant-wide head, one source, typed user sources and the request",
            "pass": all(f_ok) and all(c_viol), "fenced": fenced, "controls": controls, "fenced_ok": sum(f_ok), "fenced_cases": len(f_ok),
            "controls_violated": sum(c_viol), "control_cases": len(c_viol),
            "patches": {k: "events_guard with one check removed: " + k for k in F5_CONTROLS} | {"d1_same_key_different_content": "request fingerprint constant"}}


# ---------------------------------------------------------------- F6: liveness
def f6_takeover(arm, stall=20.0):
    conn, ks = db.connect(), K.KeyService()
    t = tenant("f6t")
    a = session(conn, t)
    res = {}
    patch = (["alter role wp_app reset idle_in_transaction_session_timeout"], ["alter role wp_app set idle_in_transaction_session_timeout = '5s'"]) if arm == "control" else NOOP
    with patched(*patch):
        zc = db.connect()                                    # zombie's connection opened under the arm's role setting

        def zombie():
            def hk(name, data=None):
                if name == "before_commit":
                    res["t_stall_start"] = time.time()
                    time.sleep(stall)
            try:
                res["zombie"] = log.append(zc, K.KeyService(), a, [msg(0, "zombie", "U")], hook=hk)
            except Exception as e:  # noqa: BLE001
                res["zombie"] = f"error {type(e).__name__}"

        z = threading.Thread(target=zombie); z.start()
        time.sleep(1.0)
        t0 = time.time()
        b, attempts = L.acquire_wait(db.connect(), t, L.session_scope("s1"), 30, force=True, deadline_s=60)
        res["takeover_s"] = round(time.time() - t0, 2); res["attempts"] = attempts
        z.join(stall + 10)
    zev = conn.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0]
    holds = res["takeover_s"] <= 8.0
    return holds, f"takeover {res['takeover_s']}s", {"stall_s": stall, "takeover_s": res["takeover_s"], "lock_timeout_retries": attempts,
                                                       "zombie_outcome": str(res.get("zombie"))[:80], "zombie_events_committed": zev}


def f6_quarantine(arm):
    conn, ks = db.connect(), K.KeyService()
    t = tenant("f6q"); s = session(conn, t)
    stale = L.Lease(t, s.scope, s.epoch - 1, "z", None)
    ap, rv = variant("quarantine_guard", "cap constant bigint := 1000;", "cap constant bigint := 1000000000;")
    with patched(ap, rv) if arm == "control" else patched(*NOOP):
        for i in range(15):
            log.append(conn, ks, stale, [msg(j, "spam", "U") for j in range(100)])
    kept = conn.execute("select count(*) from quarantine where tenant_id=%s", (t,)).fetchone()[0]
    q = db.admin().execute("select kept, dropped from quarantine_quota where tenant_id=%s", (t,)).fetchone()
    holds = kept <= 1000
    return holds, f"kept {kept}", {"attempts": 1500, "kept": kept, "quota_row": q}


def f6_xmin(mitigated, hold_s=20.0):
    """Measurement: a writing transaction left open in ANOTHER database by ANOTHER role (the admin) stalls projection
    for every tenant (pg_snapshot_xmin is cluster-wide). Mitigation: cluster-wide idle_in_transaction_session_timeout."""
    adm = db.admin(db="postgres")
    adm.execute("drop database if exists wp_scratch_xmin"); adm.execute("create database wp_scratch_xmin")
    if mitigated:
        adm.execute("alter system set idle_in_transaction_session_timeout = '5s'"); adm.execute("select pg_reload_conf()")
    try:
        other = db.admin(db="wp_scratch_xmin", autocommit=False)
        other.execute("create table t(x int)"); other.commit()
        other.execute("insert into t values (1)")            # holds an XID, then sits idle in transaction
        conn, ks, es = env()
        t = tenant("f6x"); s = session(conn, t)
        log.append(conn, ks, s, [msg(i, f"e{i}", "U") for i in range(5)])
        def end_other():
            try:
                other.rollback(); other.close()
            except psycopg.Error:
                pass                                         # mitigated arm: the server already ended it (idle timeout)
        timer = threading.Timer(hold_s, end_other)           # the other transaction ends at hold_s
        t0 = time.time()
        timer.start()
        p = projector.Projector(conn, ks, es, t)
        stall = None
        while time.time() - t0 < hold_s + 15:
            p.step()
            if es_count_all(es, t, {"match_all": {}}) >= 5:
                stall = round(time.time() - t0, 1)
                break
            time.sleep(0.5)
        p.stop()
        timer.join()
    finally:
        if mitigated:
            adm.execute("alter system reset idle_in_transaction_session_timeout"); adm.execute("select pg_reload_conf()")
        adm.execute("drop database if exists wp_scratch_xmin")
    return {"mitigated": mitigated, "other_txn_hold_s": hold_s, "projection_delay_s": stall}


def f7():
    class Stub(ES):
        def req(self, method, path, body=None, ok=(200,), **kw):
            return 200, {"acknowledged": True, "errors": True,
                         "action_results": [{"action": {"type": "remove"}, "status": 404, "error": {"type": "aliases_not_found_exception"}}]}
    try:
        Stub().aliases([{"remove": {"index": "a", "alias": "x"}}])
        r = "returned"
    except Exception as e:  # noqa: BLE001
        r = f"raised {type(e).__name__}"
    return {"property": "an alias response with errors:true is an error (stubbed response)", "pass": r.startswith("raised"), "outcome": r}


def repeat(fn, R, arms=("fenced", "control")):
    out = {}
    for arm in arms:
        runs = []
        for _ in range(R):
            try:
                h, why, w = fn(arm)
            except Exception as e:  # noqa: BLE001
                h, why, w = None, f"ERROR {type(e).__name__}: {str(e)[:160]}", {}
            runs.append({"holds": h, "why": why, "witness": w})
        out[arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                    "errors": sum(r["holds"] is None for r in runs), "runs": runs}
    out["pass"] = out["fenced"]["holds"] == R and out["control"]["violated"] >= int(0.9 * R)
    return out


if __name__ == "__main__":
    which = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()] or ["F1S", "F3", "F4", "F5", "F6", "F7", "F1"]
    R = int(next((a for a in sys.argv[1:] if a.isdigit()), "10"))
    n100k = int(sys.argv[sys.argv.index("--n100k") + 1]) if "--n100k" in sys.argv else 100000
    for w in which:
        if w == "F1":
            res = f1_large(n100k); ok = res["property_holds"]
        elif w == "F1S":
            res = repeat(f1_sweep, R); ok = res["pass"]
        elif w == "F3":
            res = f3(); ok = res["pass"]
        elif w == "F4":
            res = f4(); ok = res["pass"]
        elif w == "F5":
            res = f5(); ok = res["pass"]
        elif w == "F6":
            res = {"takeover": repeat(f6_takeover, min(R, 3)), "quarantine": repeat(f6_quarantine, min(R, 3)),
                   "xmin_measurement": [f6_xmin(False), f6_xmin(True)]}
            ok = res["takeover"]["pass"] and res["quarantine"]["pass"]
            res["pass"] = ok
            res["note"] = "takeover and quarantine R=min(R,3) per arm (each takeover run holds 20 s); xmin is a measurement, not a PASS"
        elif w == "F7":
            res = f7(); ok = res["pass"]
        else:
            continue
        res["params"] = {"R": R, "n100k": n100k}
        print(w, "PASS" if ok else "FAIL", json.dumps({k: v for k, v in res.items() if k not in ("runs",)}, default=str)[:900], flush=True)
        write_result(f"review_fix_{w}", res)
