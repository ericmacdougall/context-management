"""User decisions of 2026-11-16 (coordinator relay, main WAL wb7ed3da806f): erasure scope includes mentions by others,
and a recorded legal hold wins. Thresholds: exp/THRESHOLDS.md amendment 3.

M  mentions: planted mentions of X by another speaker U - ones matcher m1 must find at write time, ones written before
   X's identifiers were declared (rescan must find), and ones m1 is documented NOT to find (nickname, inflection,
   initial, partial). After erasure an ORACLE (admin; decrypts every remaining ciphertext; searches ES by marker)
   checks the receipt in both directions: everything in the stated scope is gone from Postgres and Elasticsearch, and
   everything outside it is still there and is listed by the receipt as not covered, with the receipt's counts equal
   to the planted counts. Arms (each differs from fenced in ONE thing):
     fenced            shipped code
     ctl_write_matcher the write-time matcher returns no mentions (wp.mentions.scan patched in-process)
     norescan_fenced   the runner skips the rescan (do_rescan=False): the store must refuse 'done'
     ctl_norescan      the same runner, and wp_erasure_pg_residue reports mentions_unscanned = 0 (one SQL patch)
H  holds: hold before request, request before hold, hold placed after mark, and a concurrent erasure of another
   subject during the hold. Control: wp_hold_check with the check removed (one SQL patch).
usage: python -m exp.scope_hold [M H] [R]"""
import json
import sys
import threading
import time
import uuid

from exp.common import write_result
from exp.patches import patched, variant, NOOP
from exp.util import env, tenant, msg, fact, session, es_count_all
from wp import db, erasure, families as F, keys as K, mentions as M, projector


def _plant(conn, ks, t, tag):
    """Returns (markers by class, identifiers)."""
    s = session(conn, t)
    mk = lambda n: f"zq{tag}m{n:02d}x"                                     # noqa: E731  (fixed width: no marker is a prefix of another)
    cls = {"own": [mk(10)], "write_hit": [mk(1), mk(2), mk(3), mk(4)], "rescan_hit": [mk(12), mk(13)],
           "miss": [mk(5), mk(6), mk(7), mk(8)], "other_subject": [mk(9)]}
    # written BEFORE any identifier of X is declared: only the erasure rescan can find these
    M.declare(conn, ks, t, "Y", ["Yolanda Park"])
    from wp import log
    log.append(conn, ks, s, [msg(0, f"Xavier Quist again {mk(13)}", "U")])
    M.declare(conn, ks, t, "X", ["Xavier Quist", "XQ-1001"])
    log.append(conn, ks, s, [msg(1, f"invoice XQ-2002 {mk(12)}", "U")])  # XQ-2002 is declared just below, after this write
    M.declare(conn, ks, t, "X", ["XQ-2002"])
    # written against the FINAL identifier set: only the write-time matcher records these (the rescan does not revisit them)
    log.append(conn, ks, s, [
        msg(2, f"I met Xavier Quist {mk(1)} today", "U"),
        msg(3, f"XAVIER QUIST called {mk(2)}", "U"),
        msg(4, f"ticket for xq-1001 {mk(3)}", "U"),
        fact(5, "U", "Xavier Quist", "phone", mk(4)),
        msg(6, f"Xav called {mk(5)}", "U"),
        msg(7, f"Xaviers car {mk(6)}", "U"),
        msg(8, f"X. Quist {mk(7)}", "U"),
        msg(9, f"Xavier Q. {mk(8)}", "U"),
        msg(10, f"Yolanda Park said hi {mk(9)}", "U"),
        msg(11, f"my own words {mk(10)}", "X"),
    ])
    return cls


def _oracle(conn_admin, ks, es, t, markers):
    """{marker: {'pg': n plaintext hits, 'es': n docs}} - reads everything, as an auditor would."""
    found = {m: {"pg": 0, "es": 0} for m in markers}
    rows = conn_admin.execute("select event_id, subject_id, content_enc from events where tenant_id=%s and content_enc is not null", (t,)).fetchall()
    keys = ks.keys(t, [r[1] for r in rows])
    for eid, sp, enc in rows:
        p = K.open_(keys[sp], t, sp, eid, enc) or ""
        for m in markers:
            found[m]["pg"] += m in p
    for fid, sp, enc in conn_admin.execute("select fact_id, subject_id, body_enc from facts where tenant_id=%s", (t,)).fetchall():
        p = K.open_(ks.keys(t, [sp])[sp], t, sp, "fact:" + fid, enc) or ""
        for m in markers:
            found[m]["pg"] += m in p
    for m in markers:
        found[m]["es"] = es_count_all(es, t, {"multi_match": {"query": m, "fields": ["content", "value"]}})
    return found


def m_once(arm):
    conn, ks, es = env()
    t = tenant("mn")
    tag = uuid.uuid4().hex[:4]
    real_scan = M.scan
    if arm == "ctl_write_matcher":
        def no_mentions(c, k, tt, texts):                                  # PATCH: the matcher finds nothing
            return real_scan(c, k, tt, texts)[0], [[] for _ in texts]
        M.scan = no_mentions
    try:
        cls = _plant(conn, ks, t, tag)
    finally:
        M.scan = real_scan
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    # PATCH (ctl_norescan): the store's rescan gate removed in both places it is enforced (residue count, identifier retention)
    a1, r1 = variant("wp_erasure_pg_residue", "'pg:mentions_unscanned', (select case when coalesce(jb.rescanned_xmax, -1) >= coalesce(jb.ident_xmax, 0) then 0",
                     "'pg:mentions_unscanned', (select case when true then 0")
    a2, r2 = variant("wp_erasure_cascade", "if held = 0 and (select coalesce(rescanned_xmax, -1) >= coalesce(ident_xmax, 0) from erasure_jobs where job_id = jid) then",
                     "if held = 0 then")
    ap, rv = a1 + a2, r2 + r1
    with patched(ap, rv) if arm == "ctl_norescan" else patched(*NOOP):
        rec = erasure.erase(conn, ks, es, t, "X", do_rescan=arm not in ("norescan_fenced", "ctl_norescan"), physical="wait")
    allm = [m for v in cls.values() for m in v]
    o = _oracle(db.admin(), ks, es, t, allm)
    present = lambda m: o[m]["pg"] + o[m]["es"] > 0                      # noqa: E731
    in_scope = cls["own"] + cls["write_hit"] + cls["rescan_hit"]
    out_scope = cls["miss"] + cls["other_subject"]
    sc = (rec.get("scope") or {}).get("mentions") or {}
    over = [m for m in in_scope if present(m)] if rec["done"] else []      # receipt says done, oracle still finds in-scope content
    under = [m for m in out_scope if not present(m)]                       # out-of-scope content wrongly removed
    counts_ok = rec["done"] and sc.get("found_at_write") == len(cls["write_hit"]) and sc.get("found_by_rescan") == len(cls["rescan_hit"]) \
        and sc.get("declared_identifiers") == 3 and len(sc.get("not_covered", [])) >= 3
    w = {"done": rec["done"], "error_type": rec["error_type"], "residue": rec.get("residue"), "scope_mentions": sc,
         "overclaimed": over, "removed_out_of_scope": under, "oracle": o,
         "steps": {k: rec["steps"].get(k) for k in ("rescan_scanned", "rescan_found", "cascade_batches", "placeholder_events")},
         "key_store": rec.get("key_store"), "planted": {k: len(v) for k, v in cls.items()}}
    if arm == "norescan_fenced":                                           # the store must refuse to claim done
        holds = rec["done"] is False and (rec.get("residue") or "").find("pg:mentions_unscanned") >= 0 and not under
        return holds, "" if holds else f"done={rec['done']} residue={rec.get('residue')}", w
    holds = rec["done"] and not over and not under and counts_ok
    return holds, "" if holds else f"done={rec['done']} over={over} under={under} counts_ok={counts_ok}", w


# ---------------------------------------------------------------- H: legal hold
def _seed(conn, ks, es, t, subjects):
    s = session(conn, t)
    from wp import log
    mk = {x: f"zqh{uuid.uuid4().hex[:6]}" for x in subjects}
    log.append(conn, ks, s, [msg(i, f"{x} says {mk[x]}", x) for i, x in enumerate(subjects)])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    return mk


def h_once(arm):
    conn, ks, es = env()
    adm = db.admin()
    out, ok = {}, []
    patch = variant("wp_hold_check", "if hs is not null then", "if false then") if arm == "control" else NOOP
    with patched(*patch):
        # H1: hold before request; concurrent erasure of Y in the same tenant completes during X's hold
        t = tenant("h1")
        mk = _seed(conn, ks, es, t, ["X", "Y"])
        hid = "hold-" + uuid.uuid4().hex[:8]
        erasure.place_hold(db.legal(), t, "X", hid, "litigation")
        res = {}
        ty = threading.Thread(target=lambda: res.__setitem__("Y", erasure.erase(db.connect(), K.KeyService(), es, t, "Y", physical="wait")))
        ty.start()
        rx = erasure.erase(conn, ks, es, t, "X", physical="wait")
        ty.join()
        x_still = es_count_all(es, t, {"match": {"content": mk["X"]}}) > 0 and ks.key_exists(t, "X")
        held_ok = (not rx["done"]) and rx.get("held") is True and x_still
        y_ok = res["Y"]["done"] is True
        time.sleep(1.0)
        sw = erasure.release_hold(db.legal(), ks, es, hid, physical="wait")
        jx = conn.execute("select state, verify from erasure_jobs where tenant_id=%s and subject_id='X'", (t,)).fetchone()
        x_after = es_count_all(es, t, {"match": {"content": mk["X"]}})
        out["H1_hold_before_request"] = {"x_held": held_ok, "x_receipt_while_held": {k: rx.get(k) for k in ("done", "error_type", "held", "holds")},
                                         "x_content_present_while_held": x_still, "y_done_during_hold": y_ok,
                                         "after_release": {"sweep": sw, "x_state": jx[0], "x_es_after": x_after}}
        ok.append(held_ok and y_ok and jx[0] == "done" and x_after == 0)
        # receipt of the released job carries the hold and the delay (store-computed)
        # H2: request before hold
        t = tenant("h2")
        mk = _seed(conn, ks, es, t, ["X"])
        jid = erasure.request(conn, t, "X")
        hid = "hold-" + uuid.uuid4().hex[:8]
        erasure.place_hold(db.legal(), t, "X", hid, "regulator")
        rx = erasure.run(conn, ks, es, jid, physical="wait")
        x_still = es_count_all(es, t, {"match": {"content": mk["X"]}}) > 0 and ks.key_exists(t, "X")
        held_ok = (not rx["done"]) and rx.get("held") is True and x_still
        time.sleep(1.0)
        erasure.release_hold(db.legal(), ks, es, hid, physical="wait")
        r2 = erasure.run(conn, ks, es, jid, physical="wait")
        out["H2_request_before_hold"] = {"x_held": held_ok, "receipt_while_held": {k: rx.get(k) for k in ("done", "error_type", "held")},
                                         "final": {k: r2.get(k) for k in ("done", "holds", "delay_s")}}
        ok.append(held_ok and r2["done"] is True and any(h.get("hold_id") == hid and h.get("released_at") for h in (r2.get("holds") or []))
                  and (r2.get("delay_s") or 0) >= 1.0)
        # H3: hold placed after mark (before the cascade): nothing further is removed, the key survives
        t = tenant("h3")
        mk = _seed(conn, ks, es, t, ["X"])
        hid = "hold-" + uuid.uuid4().hex[:8]
        placed = {}

        def hk(name, data=None):
            if name == "after_mark" and not placed:
                placed["r"] = erasure.place_hold(db.legal(), t, "X", hid, "late")
        rx = erasure.erase(conn, ks, es, t, "X", hook=hk, physical="wait")
        content_left = adm.execute("select count(*) from events where tenant_id=%s and subject_id='X' and content_enc is not null", (t,)).fetchone()[0]
        held_ok = (not rx["done"]) and rx.get("held") is True and content_left > 0 and ks.key_exists(t, "X")
        erasure.release_hold(db.legal(), ks, es, hid, physical="wait")
        r3 = erasure.erase(conn, ks, es, t, "X", physical="wait")
        out["H3_hold_after_mark"] = {"x_held": held_ok, "hold_place_result": placed.get("r"), "pg_content_left_while_held": content_left,
                                     "final_done": r3["done"]}
        ok.append(held_ok and r3["done"] is True)
    holds = all(ok)
    return holds, "" if holds else f"cases={ok}", out


def repeat(fn, R, arms):
    out = {}
    for arm in arms:
        runs = []
        for _ in range(R):
            try:
                h, why, w = fn(arm)
            except Exception as e:  # noqa: BLE001
                h, why, w = None, f"ERROR {type(e).__name__}: {str(e)[:200]}", {}
            runs.append({"holds": h, "why": why, "witness": w})
        out[arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                    "errors": sum(r["holds"] is None for r in runs), "runs": runs}
    return out


if __name__ == "__main__":
    which = [a for a in sys.argv[1:] if a in ("M", "H")] or ["M", "H"]
    R = int(next((a for a in sys.argv[1:] if a.isdigit()), "3"))
    for w in which:
        if w == "M":
            res = repeat(m_once, R, ("fenced", "ctl_write_matcher", "norescan_fenced", "ctl_norescan"))
            res["pass"] = (res["fenced"]["holds"] == R and res["norescan_fenced"]["holds"] == R
                           and res["ctl_write_matcher"]["violated"] == R and res["ctl_norescan"]["violated"] == R)
        else:
            res = repeat(h_once, R, ("fenced", "control"))
            res["pass"] = res["fenced"]["holds"] == R and res["control"]["violated"] == R
        res["params"] = {"R": R}
        summ = {a: {k: v for k, v in res[a].items() if k != "runs"} | {"why": [r["why"] for r in res[a]["runs"]][:3]} for a in res if isinstance(res[a], dict) and "runs" in res[a]}
        print(w, "PASS" if res["pass"] else "FAIL", json.dumps(summ, default=str)[:1500], flush=True)
        write_result(f"scope_hold_{w}", res)
