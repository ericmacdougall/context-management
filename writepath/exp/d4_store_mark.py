"""D4 remainder (4960f50 round): the rescan-done mark is set by the STORE, not on the runner's word.

Setup per run: Z writes 12 events (one says "lunch with yolanda") BEFORE "yolanda" is declared for Y, and Y writes one
event; then the identifier is declared and Y's erasure is requested and marked. The runner then reports the rescan
page by page (wp_erasure_rescan_page) and asks the store to set the covered version (wp_erasure_rescan_done).
  honest      erasure.rescan(page=5): every candidate reported; the store accepts (rescanned_xmax set, hit found).
  lazy        reports only the first page of 5 ids, then asks for done; fenced: refused WPS24 and rescanned_xmax stays
              unset; control (one patch: the store's missing-candidate check removed): accepted, though 7 candidates
              (the hit possibly among them) were never examined.
Thresholds (THRESHOLDS amendment 10): R=5; honest accepted 5/5; lazy fenced refused 5/5; lazy control accepted 5/5.
usage: python -m exp.d4_store_mark [R]"""
import sys

from exp.common import write_result
from exp.patches import patched, variant
from exp.util import tenant, msg, session, env
from wp import db, erasure, mentions as M


def once(mode):
    from wp import log
    conn, ks, es = env()
    t = tenant("d4m")
    s = session(conn, t)
    evs = [msg(i, ("lunch with yolanda" if i == 9 else f"note {i}"), "Z") for i in range(12)] + [msg(12, "Y own words", "Y")]
    log.append(conn, ks, s, evs)
    M.declare(conn, ks, t, "Y", ["yolanda"])
    jid = erasure.request(conn, t, "Y")
    conn.execute("select wp_erasure_mark(%s)", (jid,))
    xmax = conn.execute("select ident_xmax from erasure_jobs where job_id=%s", (jid,)).fetchone()[0]
    out = {"mode": mode}
    try:
        if mode == "honest":
            out["scanned_found"] = erasure.rescan(conn, ks, jid, page=5)
        else:
            ids = [r[0] for r in conn.execute("select event_id from events where tenant_id=%s and subject_id <> 'Y' order by event_id limit 5",
                                              (t,)).fetchall()]
            conn.execute("select wp_erasure_rescan_page(%s, %s)", (jid, ids))
            conn.execute("select wp_erasure_rescan_done(%s, %s)", (jid, xmax))
        out["accepted"] = True
    except Exception as e:  # noqa: BLE001
        out["accepted"], out["error"] = False, getattr(e, "sqlstate", None) or type(e).__name__
    out["rescanned_xmax_set"] = conn.execute("select rescanned_xmax is not null and rescanned_xmax >= ident_xmax from erasure_jobs where job_id=%s",
                                             (jid,)).fetchone()[0]
    out["examined"] = conn.execute("select count(*) from erasure_rescanned where job_id=%s", (jid,)).fetchone()[0]
    return out


def main(r):
    res = {"R": r, "honest": [once("honest") for _ in range(r)], "lazy_fenced": [once("lazy") for _ in range(r)]}
    ap, rv = variant("wp_erasure_rescan_done", "if missing > 0 then", "if false then")
    with patched(ap, rv):
        res["lazy_control"] = [once("lazy") for _ in range(r)]
    res["patch"] = "control: wp_erasure_rescan_done without the missing-candidate check"
    why = []
    if not all(x["accepted"] and x["rescanned_xmax_set"] and x["examined"] == 12 for x in res["honest"]):
        why.append(f"honest not accepted {r}/{r}")
    if not all((not x["accepted"]) and x.get("error") == "WPS24" and not x["rescanned_xmax_set"] for x in res["lazy_fenced"]):
        why.append(f"lazy fenced not refused {r}/{r}")
    if not all(x["accepted"] and x["rescanned_xmax_set"] and x["examined"] == 5 for x in res["lazy_control"]):
        why.append(f"lazy control not accepted {r}/{r} (control does not discriminate)")
    res["verdict"] = {"pass": not why, "why": why}
    print("d4_store_mark", res["verdict"], flush=True)
    write_result("d4_store_mark", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
