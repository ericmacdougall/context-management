"""Delta review D1: a legal hold on one subject protects that subject's content from ANOTHER subject's erasure, and only
the legal-operations role can place or release holds.

Cases per run (fresh tenant each):
  S1 reviewer's scenario: identifier "yolanda" declared for Y; X writes "met yolanda <mk>"; hold on X; erase Y.
     Holds iff: Y's job is not done with error_type withheld_by_hold; the receipt lists X's event and the hold id; X's
     event still has content in Postgres and it decrypts to text containing <mk>; its ES document is not a placeholder;
     Y's own event is erased (content NULL). After the legal role releases the hold, a sweep completes Y (done) and X's
     mentioning event is then removed.
  S2 mirror: hold on Y (the mentioned subject); erase X whose own message mentions Y and who has a second message that
     mentions nobody. Holds iff X's job waits (withheld_by_hold) with the mentioning event intact and the other message
     erased; after release X is done.
  S3 the application role cannot release or place a hold (refused with insufficient_privilege).
Controls (one patch each, reverted): ctl_apply_ignores_holds (wp_erasure_apply without the hold join) must violate S1
and S2; ctl_app_can_release (EXECUTE on wp_hold_release granted to wp_app) must violate S3.
usage: python -m exp.d1_hold_mentions [R]"""
import json
import sys
import uuid

import psycopg

from exp.common import write_result
from exp.patches import variant, patched
from exp.util import tenant, msg, session, env
from wp import db, erasure, keys as K, mentions as M, projector, families as F

NOOP = ([], [])
HOLD_JOIN = "and (h.subject_id = e.subject_id or h.subject_id = any(e.mentions)"


def _es_doc(es, t, eid):
    s, j = es.req("GET", f"/{F.read_alias(t, 'events')}/_doc/{eid}", ok=(200, 404))
    return j.get("_source") if s == 200 else None


def _content(adm, ks, t, eid):
    r = adm.execute("select subject_id, content_enc from events where tenant_id=%s and event_id=%s", (t, eid)).fetchone()
    if r is None or r[1] is None:
        return None
    k = ks.keys(t, [r[0]])[r[0]]
    return K.open_(k, t, r[0], eid, r[1]) if k else None


def s1(arm):
    from wp import log
    conn, ks, es = env()
    adm = db.admin()
    t = tenant("d1a")
    M.declare(conn, ks, t, "Y", ["yolanda"])
    s = session(conn, t)
    mk = "zqd1" + uuid.uuid4().hex[:6]
    rx = log.append(conn, ks, s, [msg(0, f"met yolanda {mk}", "X")])["accepted"][0]
    ry = log.append(conn, ks, s, [msg(1, f"Y own words {mk}y", "Y")])["accepted"][0]
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    hid = "hold-" + uuid.uuid4().hex[:8]
    erasure.place_hold(db.legal(), t, "X", hid, "litigation")
    rec = erasure.erase(conn, ks, es, t, "Y")
    plain = _content(adm, ks, t, rx)
    doc = _es_doc(es, t, rx)
    y_null = adm.execute("select content_enc is null from events where tenant_id=%s and event_id=%s", (t, ry)).fetchone()[0]
    w = {"receipt": {k: rec.get(k) for k in ("done", "error_type", "withheld", "queued_until_release")},
         "x_event_plain_has_marker": bool(plain and mk in plain), "x_es_doc_placeholder": bool(doc and doc.get("erased")), "y_own_content_null": y_null}
    listed = any(x["event_id"] == rx and hid in x["hold_ids"] for x in (rec.get("withheld") or []))
    while_held = (not rec["done"]) and rec.get("error_type") == "withheld_by_hold" and listed and w["x_event_plain_has_marker"] \
        and not w["x_es_doc_placeholder"] and y_null
    erasure.release_hold(db.legal(), ks, es, hid)
    st = adm.execute("select state from erasure_jobs where tenant_id=%s and subject_id='Y'", (t,)).fetchone()[0]
    after = {"y_state": st, "x_event_content_after": _content(adm, ks, t, rx) is not None}
    w["after_release"] = after
    return while_held and st == "done" and not after["x_event_content_after"], w


def s2(arm):
    from wp import log
    conn, ks, es = env()
    adm = db.admin()
    t = tenant("d1b")
    M.declare(conn, ks, t, "Y", ["yolanda"])
    s = session(conn, t)
    mk = "zqd2" + uuid.uuid4().hex[:6]
    r1 = log.append(conn, ks, s, [msg(0, f"I told yolanda {mk}", "X")])["accepted"][0]
    r2 = log.append(conn, ks, s, [msg(1, f"weather is fine {mk}b", "X")])["accepted"][0]
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    hid = "hold-" + uuid.uuid4().hex[:8]
    erasure.place_hold(db.legal(), t, "Y", hid, "regulator")
    rec = erasure.erase(conn, ks, es, t, "X")
    c1 = adm.execute("select content_enc is not null from events where tenant_id=%s and event_id=%s", (t, r1)).fetchone()[0]
    c2 = adm.execute("select content_enc is not null from events where tenant_id=%s and event_id=%s", (t, r2)).fetchone()[0]
    w = {"receipt": {k: rec.get(k) for k in ("done", "error_type", "withheld")}, "mentioning_event_content_kept": c1, "other_event_content_kept": c2}
    listed = any(x["event_id"] == r1 and hid in x["hold_ids"] for x in (rec.get("withheld") or []))
    while_held = (not rec["done"]) and rec.get("error_type") == "withheld_by_hold" and listed and c1 and not c2
    erasure.release_hold(db.legal(), ks, es, hid)
    st = adm.execute("select state from erasure_jobs where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0]
    w["after_release_state"] = st
    return while_held and st == "done", w


def s3(arm):
    conn, ks, es = env()
    t = tenant("d1c")
    hid = "hold-" + uuid.uuid4().hex[:8]
    erasure.place_hold(db.legal(), t, "X", hid, "litigation")
    out = {}
    for name, sql, args in (("release", "select wp_hold_release(%s)", (hid,)),
                            ("place", "select wp_hold_place(%s::tenant_t, %s, %s, %s)", (t, "Z", "hold-" + uuid.uuid4().hex[:8], "x"))):
        try:
            with conn.transaction():
                conn.execute(sql, args)
                out[name] = "accepted"
                raise _Rollback()
        except _Rollback:
            pass
        except psycopg.errors.InsufficientPrivilege:
            out[name] = "refused:insufficient_privilege"
    still = db.admin().execute("select released_at is null from legal_holds where hold_id=%s", (hid,)).fetchone()[0]
    erasure.release_hold(db.legal(), ks, es, hid)
    return out.get("release", "").startswith("refused") and out.get("place", "").startswith("refused") and still, out


class _Rollback(Exception):
    pass


CASES = {"S1": s1, "S2": s2, "S3": s3}
CONTROLS = {
    "ctl_apply_ignores_holds": (("S1", "S2"), lambda: variant("wp_erasure_apply", HOLD_JOIN, "and false and (h.subject_id = e.subject_id or h.subject_id = any(e.mentions)")),
    "ctl_app_can_release": (("S3",), lambda: (["grant execute on function wp_hold_release(text), wp_hold_place(tenant_t, text, text, text) to wp_app"],
                                              ["revoke execute on function wp_hold_release(text), wp_hold_place(tenant_t, text, text, text) from wp_app"])),
}


def main(R):
    res = {"fenced": {}, "controls": {}}
    for c, fn in CASES.items():
        runs = []
        for _ in range(R):
            try:
                h, w = fn("fenced")
                runs.append({"holds": h, "witness": w})
            except Exception as e:  # noqa: BLE001
                runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        res["fenced"][c] = {"holds": sum(r["holds"] is True for r in runs), "errors": sum(r["holds"] is None for r in runs), "runs": runs}
    for name, (cases, mk) in CONTROLS.items():
        ap, rv = mk()
        for c in cases:
            runs = []
            for _ in range(R):
                with patched(ap, rv):
                    try:
                        h, w = CASES[c]("control")
                        runs.append({"holds": h, "witness": w})
                    except Exception as e:  # noqa: BLE001
                        runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            res["controls"][f"{name}:{c}"] = {"violated": sum(r["holds"] is False for r in runs), "errors": sum(r["holds"] is None for r in runs),
                                             "patch": ap, "runs": runs}
    res["R"] = R
    res["pass"] = all(v["holds"] == R for v in res["fenced"].values()) and all(v["violated"] == R for v in res["controls"].values())
    print("D1", "PASS" if res["pass"] else "FAIL", json.dumps({"fenced": {k: (v["holds"], v["errors"]) for k, v in res["fenced"].items()},
                                                             "controls": {k: (v["violated"], v["errors"]) for k, v in res["controls"].items()}}))
    if not res["pass"]:
        for k, v in list(res["fenced"].items()) + list(res["controls"].items()):
            print(" ", k, json.dumps(v["runs"][0], default=str)[:900])
    write_result("d1_hold_mentions", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
