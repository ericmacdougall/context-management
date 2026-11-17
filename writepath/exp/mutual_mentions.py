"""Two erasures of subjects who mention each other (THRESHOLDS amendment 16; low item of the 7f6536c round).

Per run, a fresh tenant: identifiers declared for A ("albertina") and B ("bartholomew"); events
  A1 A's own, mentions B     A2 A's own     B1 B's own, mentions A     B2 B's own
  C1 C's own, mentions nobody                C2 C's own, mentions A and B
projected before erasure; both erasures with physical="wait".
Property: both jobs done, error_type None; A1 A2 B1 B2 C2 content NULL in Postgres and no non-placeholder ES document;
C1 still decrypts to its text and its ES document is not a placeholder.
SEQ: erase A then B in this process. CONC: two real processes, one per subject, held at a barrier on the named hook
after_mark until both have marked; overlap witness = the cascade intervals [release, after_cascade] intersect on the
database clock. Control (one patch, reverted): wp_erasure_cascade with its mention branch removed, SEQ mode.
usage: python -m exp.mutual_mentions [R_SEQ R_CONC R_CTL]   (default 5 10 5)"""
import json
import multiprocessing as mp
import sys
import uuid

from exp.common import write_result
from exp.patches import variant, patched
from exp.races import _now
from exp.util import tenant, msg, session, env
from wp import db, erasure, keys as K, mentions as M, projector, families as F

IDS = {"A": "albertina", "B": "bartholomew"}
MENTION_BRANCH = "cand := array(select event_id from erasure_mentions where tenant_id = j.tenant_id"
CTL = variant("wp_erasure_cascade", MENTION_BRANCH,
              "cand := array(select event_id from erasure_mentions where false and tenant_id = j.tenant_id")


def _es_doc(es, t, eid):
    s, j = es.req("GET", f"/{F.read_alias(t, 'events')}/_doc/{eid}", ok=(200, 404))
    return j.get("_source") if s == 200 else None


def _plain(adm, ks, t, eid):
    r = adm.execute("select subject_id, content_enc from events where tenant_id=%s and event_id=%s", (t, eid)).fetchone()
    if r is None or r[1] is None:
        return None
    k = ks.keys(t, [r[0]])[r[0]]
    return K.open_(k, t, r[0], eid, r[1]) if k else None


def build():
    from wp import log
    conn, ks, es = env()
    t = tenant("mm")
    for subj, ident in IDS.items():
        M.declare(conn, ks, t, subj, [ident])
    s = session(conn, t)
    mk = "zqmm" + uuid.uuid4().hex[:6]
    texts = {"A1": ("A", f"albertina here, spoke with bartholomew {mk}1"), "A2": ("A", f"own note one {mk}2"),
             "B1": ("B", f"bartholomew says albertina called {mk}3"), "B2": ("B", f"own note two {mk}4"),
             "C1": ("C", f"the weather is fine {mk}5"), "C2": ("C", f"carol met albertina and bartholomew {mk}6")}
    ids = {}
    for i, (name, (subj, text)) in enumerate(texts.items()):
        ids[name] = log.append(conn, ks, s, [msg(i, text, subj)])["accepted"][0]
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    return t, ids, texts["C1"][1]


def judge(t, ids, c1_text, recs):
    conn, ks, es = env()
    adm = db.admin()
    w = {"jobs": {s: {"done": r.get("done"), "error_type": r.get("error_type"), "receipt_state": r.get("receipt_state")} for s, r in recs.items()}}
    gone = {}
    for name in ("A1", "A2", "B1", "B2", "C2"):
        null = adm.execute("select content_enc is null from events where tenant_id=%s and event_id=%s", (t, ids[name])).fetchone()[0]
        doc = _es_doc(es, t, ids[name])
        gone[name] = bool(null) and (doc is None or bool(doc.get("erased")))
    c1_doc = _es_doc(es, t, ids["C1"])
    w["erased"] = gone
    w["c1_intact_pg"] = _plain(adm, ks, t, ids["C1"]) == c1_text
    w["c1_es_live"] = bool(c1_doc) and not c1_doc.get("erased")
    ok_jobs = all(j["done"] is True and j["error_type"] is None for j in w["jobs"].values())
    return ok_jobs and all(gone.values()) and w["c1_intact_pg"] and w["c1_es_live"], w


def seq_once():
    t, ids, c1 = build()
    conn, ks, es = env()
    recs = {s: erasure.erase(conn, ks, es, t, s, physical="wait") for s in ("A", "B")}
    return judge(t, ids, c1, recs)


def _child(t, subj, barrier, q):
    try:
        conn, ks, es = env()
        marks = {}

        def hk(name, data=None):
            if name == "after_mark" and "release" not in marks:
                barrier.wait(120)
                marks["release"] = _now(conn)
            elif name == "after_cascade" and "cascade_end" not in marks:
                marks["cascade_end"] = _now(conn)
        rec = erasure.erase(conn, ks, es, t, subj, hook=hk, physical="wait")
        if "cascade_end" not in marks:         # amendment 16a: a failed cascade's interval ends when erase() returns
            marks["cascade_end"], marks["cascade_end_from"] = _now(conn), "erase_return"
        q.put((subj, {k: rec.get(k) for k in ("done", "error_type", "receipt_state")}, marks, None))
    except Exception as ex:  # noqa: BLE001
        q.put((subj, {}, {}, f"{type(ex).__name__}: {str(ex)[:160]}"))


def conc_once():
    t, ids, c1 = build()
    q, bar = mp.Queue(), mp.Barrier(2)
    ps = [mp.Process(target=_child, args=(t, s, bar, q)) for s in ("A", "B")]
    for p in ps:
        p.start()
    for p in ps:
        p.join(900)
    got = {s: (r, m, e) for s, r, m, e in (q.get(timeout=10) for _ in ps)}
    errs = [e for _, _, e in got.values() if e]
    if errs:
        return None, {"errors": errs}
    (ra, ma, _), (rb, mb, _) = got["A"], got["B"]
    ov = bool(ma.get("release") and mb.get("release") and ma.get("cascade_end") and mb.get("cascade_end")
              and max(ma["release"], mb["release"]) <= min(ma["cascade_end"], mb["cascade_end"]))
    holds, w = judge(t, ids, c1, {"A": ra, "B": rb})
    w["overlap"], w["instants"] = ov, {"A": ma, "B": mb}
    return holds, w


def _arm(fn, r):
    runs = []
    for _ in range(r):
        try:
            h, w = fn()
        except Exception as ex:  # noqa: BLE001
            h, w = None, {"error": f"{type(ex).__name__}: {str(ex)[:160]}"}
        runs.append({"holds": h, **w})
    return {"R": r, "holds": sum(x["holds"] is True for x in runs), "violated": sum(x["holds"] is False for x in runs),
            "errors": sum(x["holds"] is None for x in runs), "overlapped": sum(bool(x.get("overlap")) for x in runs), "runs": runs}


def main(r_seq, r_conc, r_ctl):
    out = {"seq": _arm(seq_once, r_seq), "conc": _arm(conc_once, r_conc)}
    with patched(*CTL):
        out["control"] = _arm(seq_once, r_ctl)
    out["control_patch"] = "wp_erasure_cascade: mention branch reads no rows (erasure_mentions never used)"
    s, c, k = out["seq"], out["conc"], out["control"]
    if s["errors"] or s["violated"] or c["errors"] or c["violated"]:
        out["verdict"] = "FAIL"
    elif c["overlapped"] < 5:
        out["verdict"] = "INCONCLUSIVE"
    else:
        out["verdict"] = "PASS" if (s["holds"] == r_seq and c["holds"] == r_conc and k["violated"] == r_ctl) else "FAIL"
    print("mutual_mentions", out["verdict"], json.dumps({a: {x: out[a][x] for x in ("R", "holds", "violated", "errors", "overlapped")}
                                                        for a in ("seq", "conc", "control")}), flush=True)
    write_result("mutual_mentions", out)


if __name__ == "__main__":
    mp.set_start_method("spawn")
    a = [int(x) for x in sys.argv[1:4]] or [5, 10, 5]
    main(*(a + [5, 10, 5][len(a):]))
