"""N1, N2, N5, N8 on the per-document erasure write path (thresholds: exp/THRESHOLDS.md and amendment 3). Each control
is the shipped system with ONE named patch. Superseded and removed: N3/N3S (no rebuild exists any more), N4 (completion
with residue: exp/review_fix.py F4), N7 (decisions: exp/review_fix.py F5); the wp_ctl database is retired.
usage: python -m exp.n_new [R] [N1 N2 ...]"""
import json
import sys
import threading

from exp.common import write_result
from exp.patches import patched, variant, NOOP
from exp.util import env, tenant, msg, fact, session, es_count_all, es_count_live
from wp import db, erasure, families as F, keys as K, lease as L, log, projector, reader, registry

CLOCK = db.connect(application_name="wp-clock")


def now():
    return CLOCK.execute("select clock_timestamp()").fetchone()[0]


def Xq(subj="X"):
    return {"term": {"subject_id": subj}}


def _delete_placeholders():
    """PATCH (N1 control): erasure removes ES documents with DELETE instead of placeholder documents at version 2^62."""
    real = F.place_placeholders

    def as_delete(es_, t_, f, ids, batch=1000):
        F.ensure_family(es_, t_, f)
        if ids:
            es_.bulk_fenced([({"delete": {"_index": F.erase_alias(t_, f), "_id": d}}, None) for d in ids])
        return len(ids), 0
    return real, as_delete


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
            real, as_delete = _delete_placeholders()
            if arm == "control":
                F.place_placeholders = as_delete
            try:
                w["erase"] = erasure.erase(db.connect(), ks, es, t, "X")
            finally:
                F.place_placeholders = real
            w["t_erase_done"] = now()
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
        if name == "after_mark":
            raise RuntimeError("injected: stop after the mark")
    erasure.erase(conn, ks, es, t, "X", hook=hook)
    state = conn.execute("select state from erasure_jobs where tenant_id=%s", (t,)).fetchone()[0]
    raw = es_count_live(es, t, "facts", Xq()) + es_count_live(es, t, "events", Xq())
    R = UnfilteredReader if arm == "control" else reader.Reader
    r = R(conn, es, t)
    got = len(r.facts("Xavier")) + len(r.facts("Xavier", "diagnosis", as_of="2026-03-02T00:00:00Z")) + len(r.search("zqn5msg"))
    erasure.sweep(conn, ks, es, tenant=t)
    return got == 0, ("erased fact/event served while pending" if got else ""), {"job_state_while_pending": state, "raw_es_docs_of_X": raw, "served": got}


# ------------------------------------------------------------------ N8: a derive that read X before the erasure writes after it
# Two independent layers stop this: the store (subject_not_erased on facts/inbox) and crypto-shred (the stale derive
# cannot decrypt once the key is gone). To test the STORE layer alone, both arms keep the key (destroy_key=False);
# the job then cannot reach 'done' (keys:subject_keys=1), so the property is residue-only.
def n8(arm):
    conn, ks, es = env()
    t = tenant("n8")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(0, "X says zqn8x", "X"), msg(1, "Y says zqn8y", "Y")])
    p = projector.Projector(db.connect(application_name="n8-derive"), K.KeyService(), es, t)
    p.run()
    log.append(conn, ks, s, [fact(2, "X", "Xavier", "diagnosis", "zqn8flu")])
    res = {}

    def dhook(name, data=None):
        if name == "derive_after_read" and "erase" not in res:
            res["derive_read_has_X"] = any(r[1] == "X" for r in data)
            res["t_read"] = now()
            rec = erasure.erase(db.connect(), ks, es, t, "X", destroy_key=False)
            res["erase"] = rec["state_reached"] if "state_reached" in rec else rec.get("residue")
            res["t_erase_done"] = now()
            try:
                log.inbox_enqueue(db.connect(), K.KeyService(), s, "iX", "s1", "X", {"text": "zqn8inbox"})
                res["inbox_after_erasure"] = "accepted"
            except Exception as e:  # noqa: BLE001
                res["inbox_after_erasure"] = getattr(e, "sqlstate", type(e).__name__)
    p.hook = dhook
    try:
        res["derive"] = p.derive()
    except Exception as e:  # noqa: BLE001
        res["derive"] = type(e).__name__
    res["t_write"] = now()
    p.hook = lambda *a: None
    p.run(); p.stop()
    resid = db.admin().execute("select wp_erasure_pg_residue(%s, 'X')", (t,)).fetchone()[0]
    bad = {k: v for k, v in resid.items() if v}
    es_x = es_count_all(es, t, Xq())
    holds = not bad and es_x == 0
    return holds, "" if holds else f"residue {bad} es {es_x}", {**res, "residue_after": resid, "es_X_after": es_x,
                                                               "overlap": res["t_read"] < res["t_erase_done"] < res["t_write"]}


SCEN = {"N1": n1, "N2": n2, "N5": n5, "N8": n8}
# N8 control: subject_not_erased() (the trigger on facts and inbox) with its check removed.
PATCH = {"N8": lambda: variant("subject_not_erased", "if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;", "")}
LABEL = {"N1": "place_placeholders issues DELETEs", "N2": "a writer for an unregistered family", "N5": "reader without the filter",
         "N8": "subject_not_erased() without its check"}


def run(name, R):
    arms = {}
    for arm in ("fenced", "control"):
        runs = []
        patch = PATCH[name]() if arm == "control" and name in PATCH else NOOP
        with patched(*patch):
          for i in range(R):
            try:
                holds, why, w = SCEN[name](arm)
            except Exception as e:  # noqa: BLE001
                holds, why, w = None, f"ERROR {type(e).__name__}: {str(e)[:160]}", {}
            runs.append({"holds": holds, "why": why, "witness": w})
        arms[arm] = {"holds": sum(1 for r in runs if r["holds"] is True), "violated": sum(1 for r in runs if r["holds"] is False),
                     "errors": sum(1 for r in runs if r["holds"] is None), "runs": runs}
    ok = arms["fenced"]["holds"] == R and arms["control"]["violated"] >= int(0.9 * R)
    return {"pass": ok, "R": R, "control_patch": LABEL[name], "fenced_holds": arms["fenced"]["holds"], "control_violated": arms["control"]["violated"],
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
