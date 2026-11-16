"""Cross-tenant projection delay from one long transaction (coordinator item 1; THRESHOLDS amendment 8).

The projector reads only rows committed below pg_snapshot_xmin (row 23's late-commit fence). That xmin is the oldest
RUNNING TRANSACTION ID in the whole cluster, so any transaction that holds an xid, in any database and any tenant,
holds every tenant's projection back for as long as it stays open. A read-only snapshot holds no xid and does not.

Tenant B appends one event every 0.5 s and projects continuously; we record, per event, the seconds from its commit to
its visibility in ES. Meanwhile one HOLDER keeps a transaction open for HOLD_S seconds in tenant A (or in the key
database). Holders:
  ro_verifier        verifier, REPEATABLE READ snapshot, no xid, idle (control for the mechanism: expect no stall)
  idle_xid_app       app role, txid_current(), then idle
  active_xid_app     app role, txid_current(), then one long statement (pg_sleep)
  idle_xid_verifier  verifier role, txid_current(), then idle
  idle_xid_keysdb    key-store role in the KEY database, txid_current(), then idle (cross-database)
Arms: fenced = shipped role settings (every login role: statement_timeout and idle_in_transaction_session_timeout,
wp/setup.py ROLE_TIMEOUTS); control = those settings reset on every role except the app's pre-round values.
Max stall per holder is reported. usage: python -m exp.xmin_stall [HOLD_S]"""
import json
import multiprocessing as mp
import sys
import time

from exp.common import write_result
from exp.patches import patched
from exp.util import tenant, msg, session, env, es_count_all

HOLD_S = 60
FENCED_R, CONTROL_R = 3, 1


def _probe(stop, q):
    from wp import log, projector
    conn, ks, es = env()
    t = tenant("xsb")
    s = session(conn, t, "b1", ttl=600)
    p = projector.Projector(conn, ks, es, t, ttl_s=600, batch=200)
    out, n = [], 0
    while not stop.is_set():
        log.append(conn, ks, s, [msg(n, f"b {n}", "B1")])
        t1 = time.time()
        while es_count_all(es, t, {"match_all": {}}) < n + 1 and time.time() - t1 < 240 and not stop.is_set():
            p.step(); time.sleep(0.05)
        out.append((round(t1, 2), round(time.time() - t1, 3)))
        n += 1
        time.sleep(0.5)
    p.stop()
    q.put(out)


def _hold(kind, hold_s, q):
    from wp import config, db
    t0 = time.time()
    try:
        if kind == "ro_verifier":
            c = db.verifier(autocommit=False)
            c.execute("set transaction isolation level repeatable read"); c.execute("select count(*) from events")
        elif kind == "idle_xid_app":
            c = db.connect(autocommit=False); c.execute("select txid_current()")
        elif kind == "active_xid_app":
            c = db.connect(autocommit=False); c.execute("select txid_current()"); c.execute("select pg_sleep(%s)", (hold_s,))
        elif kind == "idle_xid_verifier":
            c = db.verifier(autocommit=False); c.execute("select txid_current()")
        elif kind == "idle_xid_keysdb":
            c = db.keys_connect(autocommit=False); c.execute("select txid_current()")
        while time.time() - t0 < hold_s:
            time.sleep(0.5)
        c.execute("select 1")             # fails if the server ended the transaction or session
        c.rollback()
        q.put((kind, "held_full", round(time.time() - t0, 2)))
    except Exception as e:  # noqa: BLE001
        q.put((kind, f"ended_by_server:{getattr(e, 'sqlstate', None) or type(e).__name__}", round(time.time() - t0, 2)))


KINDS = ["ro_verifier", "idle_xid_app", "active_xid_app", "idle_xid_verifier", "idle_xid_keysdb"]


def run_arm(hold_s):
    stop, q = mp.Event(), mp.Queue()
    pr = mp.Process(target=_probe, args=(stop, q)); pr.start()
    time.sleep(5)
    wins = {}
    for k in KINDS:
        hq = mp.Queue()
        a = time.time()
        h = mp.Process(target=_hold, args=(k, hold_s, hq)); h.start()
        res = hq.get(timeout=hold_s + 400)
        h.join(30)
        time.sleep(3)
        wins[k] = (a, time.time(), res)
    stop.set()
    series = q.get(timeout=600)
    pr.join(60)
    out = {}
    for k, (a, b, res) in wins.items():
        pts = [d for ts, d in series if a <= ts <= b]
        out[k] = {"holder": res[1], "holder_s": res[2], "samples": len(pts), "max_visible_s": max(pts) if pts else None}
    out["baseline_max_visible_s"] = max([d for ts, d in series if ts < wins[KINDS[0]][0]] or [None])
    return out


def main(hold_s):
    from wp import setup
    roles = [r for r in setup.LOGIN_ROLES]
    reset = [f"alter role {r} reset statement_timeout" for r in roles] + [f"alter role {r} reset idle_in_transaction_session_timeout" for r in roles] + \
        ["alter role wp_app set idle_in_transaction_session_timeout = '5s'", "alter role wp_app set statement_timeout = '300s'"]
    restore = [f"alter role {r} set statement_timeout = '{setup.ROLE_TIMEOUTS['statement_timeout']}'" for r in roles] + \
        [f"alter role {r} set idle_in_transaction_session_timeout = '{setup.ROLE_TIMEOUTS['idle_in_transaction_session_timeout']}'" for r in roles]
    res = {"hold_s": hold_s, "role_timeouts": setup.ROLE_TIMEOUTS, "reps": {"fenced": FENCED_R, "control": CONTROL_R}}
    res["fenced"] = [run_arm(hold_s) for _ in range(FENCED_R)]
    with patched(reset, restore):
        res["control"] = [run_arm(hold_s) for _ in range(CONTROL_R)]
    res["verdict"] = verdict(res)
    print("xmin_stall", json.dumps(res))
    write_result("xmin_stall", res)


def verdict(res):
    """THRESHOLDS amendment 8."""
    why = []
    for i, a in enumerate(res["fenced"]):
        for k in KINDS:
            m = a[k]["max_visible_s"]
            if m is None:
                why.append(f"fenced[{i}] {k}: no sample")
                continue
            lim = 5 if k == "ro_verifier" else (35 if k == "active_xid_app" else 10)
            if m > lim:
                why.append(f"fenced[{i}] {k}: {m} s > {lim} s")
            if k != "ro_verifier" and not a[k]["holder"].startswith("ended_by_server"):
                why.append(f"fenced[{i}] {k}: holder not ended by the server")
    for i, a in enumerate(res["control"]):
        for k in ("active_xid_app", "idle_xid_verifier", "idle_xid_keysdb"):
            m = a[k]["max_visible_s"]
            if m is None or m < 55:
                why.append(f"control[{i}] {k}: {m} s < 55 s (control did not reproduce the stall)")
    return {"pass": not why, "why": why}


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main(int(sys.argv[1]) if len(sys.argv) > 1 else HOLD_S)
