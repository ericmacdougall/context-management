"""MIG: rehearsal of ops/migrate_r3_7 (pre-R3-7 schema -> definer core) and its rollback, on the lab database (container
emlab-pg-polr-16096, database pol, which every test run reinstalls; the passfiles name database pol only).

Arms.
  A  clean history: install the OLD schema (exp/fixtures/pgcore_5a80e52.py, verbatim from 5a80e52), run random core_gen logs through the
     OLD engine on a dc_app connection (grants and revokes go through dc.apply_authority), add vault and subject-token rows, export
     the chain heads to a FileAnchor; fingerprint F0 of schema dc (catalog: tables, columns, functions with definitions, ACLs on schema,
     tables and functions; data: every table's rows hashed in key order) plus the public-schema CREATE privilege.
     forward -> every event replays to its stored code, chain equal hash for hash, state equal; the pre-migration anchor checks on the
          new schema; the rebuilt view equals MemoryState folded over the same events from genesis; dc_app cannot reach the kept old
          schema (USAGE revoked) nor any table; a real request through dc_idp + dc_app (open_session, submit) is applied.
     rollback -> fingerprint equals F0 exactly; the old engine works again.
     forward again -> same checks (the procedure is repeatable after a rollback).
  B  forged history (control): the same, then the R3-7 forgery executed on the OLD schema as dc_app (a grant event inserted directly and
     applied with dc.apply_authority, making a non-admin an admin's equal). forward must REFUSE and leave F(forged) unchanged.
  C  tampered state (control): an old head updated directly by dc_app (no event). forward must REFUSE, state unchanged.
usage: python -m exp.mig_rehearsal (lab host; Postgres env as file paths)."""
import hashlib
import json
import os
import random
import sys
import time
import types

from exp import common  # noqa: F401
from exp.common import write_result
from exp.core_gen import Gen, config as gen_config
from exp.seeds import seeds
import pol._core as CORE
from pol import pgcore as NEW
from ops import migrate_r3_7 as MIG

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pgcore_5a80e52.py")


def old_module():
    mod = types.ModuleType("pol.pgcore_5a80e52")
    mod.__file__, mod.__package__ = FIXTURE, "pol"
    sys.modules[mod.__name__] = mod
    exec(compile(open(FIXTURE, encoding="utf-8").read(), FIXTURE, "exec"), mod.__dict__)
    return mod


def env():
    return (int(os.environ["POL_PG_PORT"]), os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], os.environ["POL_PG_APP_PASSFILE"],
            os.environ["POL_PG_IDP_PASSWORD_FILE"], os.environ["POL_PG_IDP_PASSFILE"])


def owner(name):
    port, sp = env()[:2]
    return NEW._connect("postgres", sp, port, name)


def fingerprint(c, schema="dc"):
    q = lambda s, *a: c.execute(s, a).fetchall()        # noqa: E731
    out = {"public_create": q("select has_schema_privilege('public', 'public', 'CREATE')")[0][0],
           "schema_acl": q("select coalesce(nspacl::text, '') from pg_namespace where nspname = %s", schema),
           "tables": q("select c.relname, c.relkind, coalesce(c.relacl::text, ''), pg_get_userbyid(c.relowner) from pg_class c join pg_namespace n "
                       "on n.oid = c.relnamespace where n.nspname = %s order by c.relname", schema),
           "columns": q("select table_name, column_name, data_type, column_default from information_schema.columns where table_schema = %s "
                        "order by table_name, ordinal_position", schema),
           "functions": [(p, hashlib.sha256(d.encode()).hexdigest()[:16], a) for p, d, a in
                         q("select p.oid::regprocedure::text, pg_get_functiondef(p.oid), coalesce(p.proacl::text, '') from pg_proc p join "
                           "pg_namespace n on n.oid = p.pronamespace where n.nspname = %s order by 1", schema)],
           "triggers": q("select tgname, tgenabled from pg_trigger g join pg_class c on c.oid = g.tgrelid join pg_namespace n on n.oid = c.relnamespace "
                         "where n.nspname = %s and not tgisinternal order by 1", schema),
           "data": {}}
    for (t,) in q("select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = %s and c.relkind = 'r' "
                  "order by 1", schema):
        rows = c.execute('select row_to_json(x)::text from "%s"."%s" x order by 1' % (schema, t)).fetchall()
        out["data"][t] = [len(rows), hashlib.sha256("\n".join(sorted(r[0] for r in rows)).encode()).hexdigest()]
    return out


def build_old_history(OLD, seed, logs=3):
    """Install the old schema and fill it through the old engine; returns (tenants config, all events delivered)."""
    port, sp, apw, ap, _ip, _if = env()
    cfg = gen_config()
    tenants = {cfg["tenant"]: cfg}
    o = owner("mig_reset")                  # lab reset: a kept pre-migration schema from an earlier rehearsal (forward refuses while it exists)
    o.execute("drop schema if exists %s cascade" % MIG.PRE)
    o.execute("drop schema if exists dc_crypto cascade")
    o.close()
    OLD.install(port, sp, apw, tenants)
    app = OLD.connect_app(port, ap, "mig_old")
    eng = OLD.PgEngine(app, cfg["tenant"])
    rng, g, hist, codes = random.Random(seed), Gen(seed), [], {}
    st = CORE.MemoryState(cfg)
    for _ in range(logs * 100):
        ev = g.retry_or_new(hist)
        hist.append(ev)
        c, _o = eng._submit(ev)
        codes[c] = codes.get(c, 0) + 1
        mc, _mo, d = CORE.reduce(st, ev)
        st.apply_delta(d)
        for r in ("r1", "r2"):
            g.note_version(r, st.heads[r]["version"])
    app.close()
    o = owner("mig_fill")
    o.execute("insert into dc.vals values (%s,'val-x1','about dana','{}'::jsonb, array['dana']), (%s,'val-x2','other','{}'::jsonb, array[]::text[])",
              (cfg["tenant"], cfg["tenant"]))
    o.execute("insert into dc.subject_tokens values (%s,'dana','subj-%s')" % ("%s", rng.getrandbits(64)), (cfg["tenant"],))
    o.close()
    return tenants, codes, rng


def checks_after_forward(OLD, tenants, anchor_path, rep):
    port, _sp, _apw, ap, _ip, idpf = env()
    o = owner("mig_check")
    out = {"report": rep}
    t = next(iter(tenants))
    eng = NEW.PgEngine(o, t)
    out["anchor_check"] = NEW.anchor_check(o, NEW.FileAnchor(anchor_path), t)
    evs = [e for (e,) in o.execute("select ev from dc.events where tenant=%s order by chain_n", (t,)).fetchall()]
    st = CORE.MemoryState(tenants[t])
    for ev in evs:
        _c, _o, d = CORE.reduce(st, ev)
        st.apply_delta(d)
    out["view_equals_memory_fold"] = eng._comparable() == st.comparable()
    app = NEW.connect_app(port, ap, "mig_app")
    denied = {}
    for q in ("select count(*) from %s.events" % MIG.PRE, "select count(*) from dc.events", "select dc._apply_event('t1', '{}'::jsonb)"):
        try:
            with app.transaction():
                app.execute(q)
            denied[q] = False
        except Exception as e:          # noqa: BLE001
            denied[q] = type(e).__name__
    out["app_denied"] = denied
    idp = NEW.connect_idp(port, idpf, "mig_idp")
    row = o.execute("select p.pid, r.req from dc.principal p join dc.role r on r.tenant = p.tenant and r.pid = p.pid and r.role = 'decide' "
                    "where p.tenant = %s and p.kind = 'human' and not p.revoked order by r.req, p.pid limit 1", (t,)).fetchone()
    human, req = row if row else (None, None)
    if human is None:
        o.close()
        idp.close()
        app.close()
        return dict(out, ok=False, api_request="no unrevoked human with authority left")
    tok = idp.execute("select dc.open_session(%s, %s, 'owner_ui', 'mig-lab', 60)", (t, human)).fetchone()[0]
    idp.close()
    hv = eng._head(req)["version"]
    r = app.execute("select dc.submit(%s, %s::jsonb)", (tok, json.dumps({"type": "retract", "client_id": "mig-%d" % int(time.time() * 1000),
                                                                         "idem": "mig-%d" % time.time_ns(), "req": req, "based_on": hv}))).fetchone()[0]
    out["api_request"] = r["code"]
    app.close()
    o.close()
    out["ok"] = (out["anchor_check"]["ok"] and out["anchor_check"]["anchored"] > 0 and out["view_equals_memory_fold"]
                 and all(v not in (False,) for v in denied.values()) and r["code"] in CORE.OUTCOME_CODES
                 and r["code"] not in ("unauthorized", "principal_revoked", "bad_event", "tenant_mismatch", "channel_mismatch"))   # judged by the core
    return out


def arm_clean(OLD, seed, anchor_path):
    tenants, codes, _rng = build_old_history(OLD, seed)
    o = owner("mig_a")
    t = next(iter(tenants))
    if os.path.exists(anchor_path):
        os.remove(anchor_path)
    OLD.anchor_export(o, OLD.FileAnchor(anchor_path), t)
    f0 = fingerprint(o)
    res = {"seed": seed, "old_codes": codes}
    res["forward1"] = MIG.forward(o, tenants, env()[4])
    res["checks1"] = checks_after_forward(OLD, tenants, anchor_path, res["forward1"])
    try:
        MIG.forward(o, tenants, env()[4])
        res["forward_twice_refused"] = False
    except MIG.MigrationRefused as e:
        res["forward_twice_refused"] = str(e)[:120]
    MIG.rollback(o)
    f1 = fingerprint(o)
    res["rollback_restores_fingerprint"] = f1 == f0
    if f1 != f0:
        res["fingerprint_diff"] = sorted(k for k in f0 if f0[k] != f1[k]) + sorted(k for k in f0["data"] if f0["data"][k] != f1["data"].get(k))
    port, _sp, _apw, ap = env()[:4]
    app = OLD.connect_app(port, ap, "mig_old_again")
    res["old_engine_after_rollback"] = OLD.PgEngine(app, t)._verify_chain()[1]
    app.close()
    res["forward2"] = MIG.forward(o, tenants, env()[4])
    res["checks2"] = checks_after_forward(OLD, tenants, anchor_path, res["forward2"])
    MIG.finish(o)
    o.close()
    res["ok"] = (res["checks1"]["ok"] and res["checks2"]["ok"] and res["rollback_restores_fingerprint"] and res["old_engine_after_rollback"]
                 and bool(res["forward_twice_refused"]))
    return res


def arm_refused(OLD, seed, how):
    tenants, _codes, _rng = build_old_history(OLD, seed)
    t = next(iter(tenants))
    port, _sp, _apw, ap = env()[:4]
    app = OLD.connect_app(port, ap, "mig_forger")
    nonadmin = next(p for p, k in tenants[t]["principals"].items() if k == "human" and p not in tenants[t]["admins"])
    with app.transaction():
        if how == "forged_grant":
            ev = {"type": "grant", "id": "forged/1", "idem": "forged-1", "tenant": t, "principal": nonadmin, "channel": "owner_ui", "req": "r2",
                  "target": nonadmin, "role": "decide"}
            # dc_app's INSERT on dc.events at 5a80e52: a grant row the core never judged (apply_authority would refuse a non-admin author,
            # so it is not called; the row alone is the forgery). A forged row in an ADMIN's name that the rules accept is NOT detectable
            # by replay (no session provenance before R3-7): disclosed in the result's not_proven.
            app.execute("insert into dc.events(tenant, principal, idem, req, evid, fp, code, ev) values (%s,%s,%s,%s,%s,%s,'granted',%s)",
                        (t, nonadmin, ev["idem"], "r2", ev["id"], CORE.sha(ev), json.dumps(ev)))
        else:
            app.execute("update dc.head set doc = jsonb_set(doc, '{value_ref}', '\"vFORGED\"') where tenant=%s and req='r1'", (t,))
    app.close()
    o = owner("mig_ctl")
    f0 = fingerprint(o)
    try:
        MIG.forward(o, tenants, env()[4])
        refused = False
    except MIG.MigrationRefused as e:
        refused = str(e)[:200]
    except Exception as e:      # noqa: BLE001
        refused = "error:" + type(e).__name__ + ":" + str(e)[:160]
    f1 = fingerprint(o)
    o.close()
    return {"seed": seed, "control": how, "refused": refused, "unchanged": f0 == f1, "ok": bool(refused) and not str(refused).startswith("error:") and f0 == f1}


def main():
    t0 = time.time()
    OLD = old_module()
    anchor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "tmp", "mig_anchor.jsonl")
    os.makedirs(os.path.dirname(anchor), exist_ok=True)
    sd = seeds("mig")[:3]
    out = {"clean": [arm_clean(OLD, s, anchor) for s in sd]}
    out["controls"] = []
    for s in sd:
        for how in ("forged_grant", "head_tampered"):
            try:
                out["controls"].append(arm_refused(OLD, s, how))
            except Exception as e:      # noqa: BLE001
                out["controls"].append({"seed": s, "control": how, "ok": False, "error": repr(e)[:300]})
    out["pass"] = all(x["ok"] for x in out["clean"]) and all(x["ok"] for x in out["controls"])
    out["verdict"] = "PASS" if out["pass"] else "FAIL"
    out["seconds"] = round(time.time() - t0, 1)
    out["not_proven"] = ("replay detects log rows the rules refuse and state written outside the log; a forged row in the name of a principal "
                         "who had the authority is indistinguishable from a genuine one (the pre-R3-7 schema kept no session provenance); "
                         "lab database only (the passfiles name database pol); one tenant per history; a failure mid-transaction relies on "
                         "Postgres transactional DDL (no partial state was injected); roles are cluster-wide and dc_idp is left after rollback")
    p = write_result("mig_rehearsal", {"seeds": sd, "fixture": "exp/fixtures/pgcore_5a80e52.py"}, out)
    print(json.dumps({"pass": out["pass"], "clean": [{k: v for k, v in x.items() if k in ("ok", "rollback_restores_fingerprint", "fingerprint_diff",
                                                                                      "forward_twice_refused")} for x in out["clean"]],
                      "controls": [{k: x.get(k) for k in ("control", "ok", "refused", "error")} for x in out["controls"]]}, indent=1, default=str)[:4000])
    print("wrote", p)
    if not out["pass"]:
        raise SystemExit(1)                          # run_all.sh scores a nonzero exit as FAIL


if __name__ == "__main__":
    main()
