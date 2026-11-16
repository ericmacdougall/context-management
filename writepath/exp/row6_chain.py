"""Row 6: forged append, guard tampering by the app role, lost or regressed log tail against an external anchor.
Thresholds: THRESHOLDS.md amendment 5. R runs per case and arm; each run uses a fresh tenant.

  A forged append: the cluster admin (any holder of events-database write access but not of the key store) inserts an
    event with a correct SHA-256 chain link, the current lease epoch and every column the guard checks. Fenced = keyed
    chain (shipped): verify_chain is False. Control = the pre-row-6 unkeyed chain (append and verify without the key):
    the forged link verifies.
  B the app role tries to disable/drop the append-only guard, replace the guard function or set
    session_replication_role, each inside a rolled-back transaction. Fenced: all refused. Control: wp_app made a member
    of wp_owner (the old build, where the app owned its tables): at least one succeeds.
  C lost tail: anchor, then the last k events (k random 1-5) vanish together with the chain head (as a restore to an
    earlier point or a failover to a lagging replica would leave it). D regressed head: as C, then k+2 new legitimate
    events are appended (the head moves past the anchor on a different history).
    Fenced: anchor.check reports truncated (C) / rewritten (D). Control: the chain-only check reports ok.
usage: python -m exp.row6_chain [R]"""
import hashlib
import json
import random
import sys
import uuid

import psycopg

from exp.common import write_result
from exp.patches import patched
from exp.util import tenant, msg, session, env
from wp import anchor as A, db, log, mentions as M, secrets as SEC

RNG = random.Random(6006)


class _RB(Exception):
    pass


def _forge(adm, t, sid, keyed_chain):
    """Insert one event as the admin with a correct UNKEYED link (the forger has no chain key)."""
    nxt, prev = adm.execute("select next_seq, last_hash from chains where tenant_id=%s and session_id=%s", (t, sid)).fetchone()
    ep = adm.execute("select epoch from leases where tenant_id=%s and scope=%s", (t, "session:" + sid)).fetchone()[0]
    scan = adm.execute("select max_seq from identifier_epochs where tenant_id=%s", (t,)).fetchone()[0]
    enc = b"forged" + uuid.uuid4().bytes
    dg = hashlib.sha256(enc).hexdigest()
    eid = uuid.uuid4().hex
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc)
    h = log.event_hash(prev, t, sid, nxt, eid, "message", "U", "user", "user_typed", ts.isoformat(), dg, ep, None, None, None, None, [], scan, key=None)
    adm.execute("insert into events(tenant_id,session_id,seq,event_id,idem_key,req_hash,kind,subject_id,actor,trust_origin,ts_event,content_enc,content_digest,"
                "mentions,mention_scan_upto,prev_hash,hash,lease_scope,lease_epoch,secret_scan) values (%s,%s,%s,%s,%s,%s,'message','U','user','user_typed',%s,%s,%s,"
                "'{}',%s,%s,%s,%s,%s,%s)", (t, sid, nxt, eid, eid, "x", ts, enc, dg, scan, prev, h, "session:" + sid, ep, SEC.VERSION))
    return eid


def case_a(arm):
    conn, ks, es = env()
    adm = db.admin()
    t = tenant("r6a")
    s = session(conn, t)
    real = ks.chain_key
    if arm == "control":
        ks.chain_key = lambda tenant_: None            # the pre-row-6 chain: plain SHA-256 links
    try:
        log.append(conn, ks, s, [msg(i, f"real {i}", "U") for i in range(RNG.randint(2, 6))])
        _forge(adm, t, "s1", arm == "fenced")
    finally:
        ks.chain_key = real
    ok = log.verify_chain(conn, t, "s1", ks)[0] if arm == "fenced" else log.verify_chain_unkeyed(conn, t, "s1")[0]
    return (not ok), {"forged_verifies": ok}


ATTACKS = [
    ("disable_guard_trigger", "alter table events disable trigger events_guard"),
    ("disable_user_triggers", "alter table events disable trigger user"),
    ("drop_guard_trigger", "drop trigger events_guard on events"),
    ("replace_guard_function", "create or replace function events_guard() returns trigger language plpgsql as $$ begin return NEW; end $$"),
    ("replication_role_replica", "set local session_replication_role = replica"),
]


def case_b(arm):
    conn = db.connect()
    out = {}
    for name, sql in ATTACKS:
        try:
            with conn.transaction():
                conn.execute(sql)
                out[name] = "accepted"
                raise _RB()
        except _RB:
            pass
        except psycopg.Error as e:
            out[name] = "refused:" + str(e.sqlstate)
    return all(v.startswith("refused") for v in out.values()), out


def _drop_tail(adm, t, sid, k):
    with adm.transaction():
        adm.execute("alter table events disable trigger user")
        n = adm.execute("select next_seq - 1 from chains where tenant_id=%s and session_id=%s", (t, sid)).fetchone()[0]
        adm.execute("delete from events where tenant_id=%s and session_id=%s and seq > %s", (t, sid, n - k))
        h = adm.execute("select hash from events where tenant_id=%s and session_id=%s and seq=%s", (t, sid, n - k)).fetchone()[0]
        adm.execute("alter table chains disable trigger user")
        adm.execute("update chains set next_seq=%s, last_hash=%s where tenant_id=%s and session_id=%s", (n - k + 1, h, t, sid))
        for tb in ("chains", "events"):                  # restore every user trigger to ALWAYS (guard_check G3 verifies)
            for (g,) in adm.execute("select tgname from pg_trigger where tgrelid = %s::regclass and not tgisinternal", (tb,)).fetchall():
                adm.execute(f'alter table {tb} enable always trigger "{g}"')


def case_cd(arm, regress):
    conn, ks, es = env()
    adm = db.admin()
    t = tenant("r6d" if regress else "r6c")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"real {i}", "U") for i in range(10)])
    store = A.FileAnchorStore()
    A.anchor(conn, store, t)
    k = RNG.randint(1, 5)
    _drop_tail(adm, t, "s1", k)
    if regress:
        log.append(conn, ks, s, [msg(100 + i, f"new history {i}", "U") for i in range(k + 2)])
    chain_ok = log.verify_chain(conn, t, "s1", ks)[0]
    chk = A.check(conn, ks, store, t)
    kinds = sorted({f["kind"] for f in chk["findings"]})
    want = "rewritten" if regress else "truncated"
    if arm == "fenced":
        return (not chk["ok"]) and want in kinds, {"k": k, "chain_ok": chain_ok, "anchor_findings": kinds}
    return (not chain_ok), {"k": k, "chain_ok": chain_ok}           # control = chain-only: holds iff it detects


CASES = {"A_forged_append": case_a, "B_app_guard_tamper": case_b,
         "C_lost_tail": lambda arm: case_cd(arm, False), "D_regressed_head": lambda arm: case_cd(arm, True)}
CONTROL_PATCH = {"B_app_guard_tamper": (["grant wp_owner to wp_app"], ["revoke wp_owner from wp_app"])}


def main(R):
    res = {"R": R, "anchor_store": A.FileAnchorStore().describe(), "cases": {}}
    for c, fn in CASES.items():
        arms = {}
        for arm in ("fenced", "control"):
            runs = []
            ap, rv = CONTROL_PATCH.get(c, ([], [])) if arm == "control" else ([], [])
            for _ in range(R):
                with patched(ap, rv):
                    try:
                        h, w = fn(arm)
                        runs.append({"holds": h, "witness": w})
                    except Exception as e:  # noqa: BLE001
                        runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            arms[arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                         "errors": sum(r["holds"] is None for r in runs), "runs": runs}
        res["cases"][c] = arms
    res["pass"] = all(v["fenced"]["holds"] == R and v["control"]["violated"] == R for v in res["cases"].values())
    print("row6", "PASS" if res["pass"] else "FAIL", json.dumps({c: {a: (v[a]["holds"], v[a]["violated"], v[a]["errors"]) for a in v} for c, v in res["cases"].items()}))
    for c, v in res["cases"].items():
        print(" ", c, json.dumps({a: v[a]["runs"][0] for a in v}, default=str)[:700])
    write_result("row6_chain", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
