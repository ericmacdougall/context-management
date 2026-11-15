"""Reproduce review findings N1-N7 against the OLD code (a13b524), live on this stream's Postgres and Elasticsearch.

Each scenario states the property the write path must have and reports `property_holds`. On the old code every one is
expected to be False (the finding reproduces). Fault injection is by named in-process hooks (proof rule 10): the old
module function is wrapped, the erasure or lock loss is run synchronously at the exact interleaving point, and the
original function is restored. usage: python -m old_repro.n_old [N1 N2 ...]"""
import datetime as dt
import json
import sys
import time

from old_repro import oldenv
from old_repro.oldenv import odb, OldES, kill_backend
from memsys import store, derive as D, erasure, projector, esx, reader, kms as okms  # old modules
from evolution import families, taskview, decisions as OD  # old modules
from exp.common import write_result

TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def ev(i, text, subj, kind="message"):
    return dict(ts_event=TS + dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind=kind, subject_id=subj, content=text)


def fresh():
    conn, lock, es = oldenv.conns()
    return conn, lock, es


def es_count(es, target, q):
    es.req("POST", f"/{target}/_refresh", ok=(200, 404))
    s, j = es.req("POST", f"/{target}/_count", {"query": q}, ok=(200, 404))
    return j.get("count", 0) if s == 200 else 0


def setup(es, conn, T):
    esx.install_templates(es)
    families.install_templates(es)
    esx.drop_tenant(es, T)


def n1():
    """Fence pinned after the read: a projector whose lock session is lost reads X's event, the whole erasure of X runs,
    then the projector resolves its generation (fresh cache) and writes. Property: X's event is not in the live index."""
    T = "n1old"
    conn, lock, es = fresh(); setup(es, conn, T)
    store.append_events(conn, T, "s1", [ev(0, "hello zqn1y from y", "Y"), ev(1, "X says zqn1a", "X")])
    projector.Projector(conn, lock, es, tenants=[T], derive=False).run()
    store.append_events(conn, T, "s1", [ev(2, "X later says zqn1b", "X")])
    c2, l2 = odb.connect(autocommit=True), odb.connect(autocommit=True)
    p2 = projector.Projector(c2, l2, es, tenants=[T], derive=False)
    st, orig = {}, D.read_event_batch

    def hooked(*a, **k):
        xm, rows = orig(*a, **k)
        if rows and "receipt" not in st:
            st["rows_read"] = len(rows); st["read_has_X"] = any(r["subject_id"] == "X" and not r["tomb"] for r in rows)
            kill_backend(l2, conn)                                   # the projector's lock session is lost
            c3, l3 = odb.connect(autocommit=True), odb.connect(autocommit=True)
            st["receipt"] = erasure.erase_subject(c3, l3, es, T, "X")  # whole erasure runs inside the window read -> pin
            st["t_erase_done"] = time.time()
        return xm, rows
    D.read_event_batch = hooked
    try:
        p2.mirror_events(T)
    except Exception as e:  # noqa: BLE001  (the unlock on the killed session raises AFTER the bulk)
        st["projector_exc"] = type(e).__name__
    finally:
        D.read_event_batch = orig
    leaked = es_count(es, f"sys-{T}-events", {"term": {"subject_id": "X"}})
    rec = st.get("receipt", {})
    return {"property": "erased subject X absent from the live events index after erasure completes", "property_holds": leaked == 0,
            "witness": {"rows_read_before_erasure": st.get("rows_read"), "read_contained_X": st.get("read_has_X"),
                        "receipt_es_event_docs": rec.get("verification", {}).get("es_event_docs"), "job_state": job_state(conn, T),
                        "X_docs_in_live_index_after": leaked, "projector_exc": st.get("projector_exc")}}


def job_state(conn, T):
    return [r[0] for r in conn.execute("select state from erasure_jobs where tenant_id=%s order by created_at", (T,)).fetchall()]


def n2():
    """Index families outside erasure: an ev2 family mirrors events; erasing X must purge X from ev2 too."""
    T = "n2old"
    conn, lock, es = fresh(); setup(es, conn, T)
    store.append_events(conn, T, "s1", [ev(0, "hello zqn2y", "Y"), ev(1, "X says zqn2x", "X")])
    projector.Projector(conn, lock, es, tenants=[T], derive=False).run()
    fp = families.FamilyProjector(conn, es, "ev2", [T])
    while fp.run_once():
        pass
    before = es_count(es, f"sys-{T}-ev2", {"term": {"subject_id": "X"}})
    rec = erasure.erase_subject(conn, lock, es, T, "X")
    after = es_count(es, f"sys-{T}-ev2", {"term": {"subject_id": "X"}})
    return {"property": "erasure purges X from every index family (ev2 included)", "property_holds": after == 0,
            "witness": {"X_in_ev2_before": before, "X_in_ev2_after": after, "X_in_events_after": es_count(es, f"sys-{T}-events", {"term": {"subject_id": "X"}}),
                        "receipt_rebuilt": rec.get("es_rebuilt"), "job_state": job_state(conn, T)}}


def n3():
    """Unfenced rebuild: eraser A (subject X) reads survivors (incl. Y's events), loses its lock; eraser B erases Y fully;
    A then bulk-writes its survivor set by concrete index name. Property: Y absent from every index of the tenant."""
    T = "n3old"
    conn, lock, es = fresh(); setup(es, conn, T)
    store.append_events(conn, T, "s1", [ev(0, "X says zqn3x", "X"), ev(1, "Y says zqn3y", "Y"), ev(2, "Z says hello", "Z")])
    projector.Projector(conn, lock, es, tenants=[T], derive=False).run()
    cA, lA = odb.connect(autocommit=True), odb.connect(autocommit=True)
    cB, lB = odb.connect(autocommit=True), odb.connect(autocommit=True)
    st, orig = {}, store.read_plain

    def hooked(*a, **k):
        rows = orig(*a, **k)
        if "receiptB" not in st:
            st["receiptB"] = "running"
            st["A_survivors_have_Y"] = any(r["subject_id"] == "Y" for r in rows)
            kill_backend(lA, conn)                                  # A's lock session is lost
            st["receiptB"] = erasure.erase_subject(cB, lB, es, T, "Y")
        return rows
    store.read_plain = hooked
    try:
        erasure.erase_subject(cA, lA, es, T, "X")
    except Exception as e:  # noqa: BLE001
        st["A_exc"] = type(e).__name__
    finally:
        store.read_plain = orig
    y_live = es_count(es, f"sys-{T}-events", {"term": {"subject_id": "Y"}})
    y_any = es_count(es, f"sys-{T}-*", {"term": {"subject_id": "Y"}})
    rb = st.get("receiptB") if isinstance(st.get("receiptB"), dict) else {}
    return {"property": "after B erases Y, Y is absent from every index of the tenant", "property_holds": y_any == 0,
            "witness": {"A_survivor_read_had_Y": st.get("A_survivors_have_Y"), "B_receipt_es_event_docs": rb.get("verification", {}).get("es_event_docs"),
                        "Y_docs_live": y_live, "Y_docs_any_index": y_any, "A_exc": st.get("A_exc"), "job_states": job_state(conn, T)}}


def n4():
    """Completion ignores verification: the purge step leaves X's documents (modelled by disabling it); the job must not
    reach 'done', and after the job ends the reader must still hide X."""
    T = "n4old"
    conn, lock, es = fresh(); setup(es, conn, T)
    store.append_events(conn, T, "s1", [ev(0, "hello zqn4y", "Y"), ev(1, "X says zqn4x", "X")])
    projector.Projector(conn, lock, es, tenants=[T], derive=False).run()
    rec = erasure.erase_subject(conn, lock, es, T, "X", es_purge=False)
    states = job_state(conn, T)
    served = [h["id"] for h in reader.Reader(conn, es).search(T, "X says zqn4x")["hits"] if h["id"].endswith("/2")]
    ok = not ("done" in states and rec["verification"]["es_event_docs"] > 0) and not served
    return {"property": "job reaches done only with verification zero; reader hides tombstoned events after done", "property_holds": ok,
            "witness": {"job_states": states, "receipt_es_event_docs": rec["verification"]["es_event_docs"], "reader_served_X_event_after_done": served}}


def n5():
    """query_facts is unfiltered while an erasure job is pending (phase 1 committed, purge not yet run)."""
    T = "n5old"
    conn, lock, es = fresh(); setup(es, conn, T)
    store.append_events(conn, T, "s1", [ev(0, "Xavier's diagnosis is zqn5flu.", "X"), ev(1, "Yolanda's colour is blue.", "Y")])
    projector.Projector(conn, lock, es, tenants=[T], derive=True).run()
    try:
        erasure.erase_subject(conn, lock, es, T, "X", fail_at="after_phase1")
    except RuntimeError:
        pass
    r = reader.Reader(conn, es)
    qf = r.query_facts(T, "Xavier")
    sf = r.search_facts(T, "zqn5flu")["hits"]
    leaked = [f for f in qf if "zqn5flu" in json.dumps(f)]
    return {"property": "no read path serves an erased fact while the job is pending", "property_holds": not leaked,
            "witness": {"job_states": job_state(conn, T), "query_facts_hits": len(qf), "query_facts_leaked": len(leaked), "search_facts_hits": len(sf)}}


def n6():
    """Plaintext task state outside the subject key: task_view keeps the item text after the subject is erased."""
    T = "n6old"
    conn, lock, es = fresh(); setup(es, conn, T)
    taskview.setup(conn)
    taskview.append(conn, T, "s1", "task_defined", {"task_id": "T1", "items": [{"id": "a", "type": "scope", "text": "zqn6secret scope"}]})
    taskview.update_view(conn, T, "s1")
    before = conn.execute("select count(*) from task_view where tenant_id=%s and state::text like %s", (T, "%zqn6secret%")).fetchone()[0]
    try:
        erasure.erase_subject(conn, lock, es, T, "u1")
    except Exception as e:  # noqa: BLE001
        pass
    after = conn.execute("select count(*) from task_view where tenant_id=%s and state::text like %s", (T, "%zqn6secret%")).fetchone()[0]
    return {"property": "no plaintext of an erased subject remains in any Postgres table", "property_holds": after == 0,
            "witness": {"task_view_rows_with_marker_before": before, "after_erasure": after, "job_states": job_state(conn, T)}}


def n7():
    """Decisions: version binding and idempotency (pure fold of the old decisions module)."""
    def e(seq, kind, body, trust="user_typed"):
        return {"seq": seq, "kind": kind, "trust_origin": trust, "body": body}
    DEF = e(1, "task_defined", {"task_id": "T", "items": [{"id": "a", "type": "scope", "text": "EU only"}]})
    DET = e(2, "change_detected", {"change_id": "c1", "item": "a", "op": "change", "proposed_text": "US only", "quote": "q", "quote_event_id": "e",
                                   "speaker": "x", "speaker_trust": "assistant", "detector": "d", "confidence": .9}, "tool")
    RB = e(3, "read_back", {"read_back_id": "rb1", "version": 1, "includes": ["c1"]}, "assistant")
    a = OD.fold([DEF, DET, RB, e(4, "decision_recorded", {"decision_id": "d0", "action": "state", "ops": [{"op": "change", "id": "a", "text": "APAC only"}]}),
                 e(5, "decision_recorded", {"decision_id": "d1", "action": "accept_read_back", "read_back_id": "rb1"})])
    base = [DEF, DET, RB, e(4, "decision_recorded", {"decision_id": "d1", "action": "accept_read_back", "read_back_id": "rb1"})]
    did = OD.fold(base)["decisions"][-1]["decision_id"]
    b = OD.fold(base + [e(5, "decision_recorded", {"decision_id": "d2", "action": "revert", "reverts": did}),
                        e(6, "decision_recorded", {"decision_id": "d3", "action": "accept_read_back", "read_back_id": "rb1"})])
    c = OD.fold([DEF, DET, e(3, "decision_recorded", {"decision_id": "d1", "action": "accept", "change_id": "c1"}),
                 e(4, "decision_recorded", {"decision_id": "d2", "action": "revert", "reverts": "d1"}),
                 e(5, "decision_recorded", {"decision_id": "d1", "action": "accept", "change_id": "c1"})])
    sub = {"a_stale_readback_refused": a["items"]["a"]["text"] == "APAC only",
           "b_replayed_accept_refused": b["items"]["a"]["text"] == "EU only",
           "c_retry_same_id_is_noop": c["items"]["a"]["text"] == "EU only" and "c1" in c["pending"]}
    return {"property": "stale approval refused; replayed approval refused; retried decision id is a no-op", "property_holds": all(sub.values()),
            "witness": {"sub": sub, "a_text": a["items"]["a"]["text"], "b_text": b["items"]["a"]["text"], "c_text": c["items"]["a"]["text"], "c_pending": list(c["pending"])}}


SCEN = {"N1": n1, "N2": n2, "N3": n3, "N4": n4, "N5": n5, "N6": n6, "N7": n7}

if __name__ == "__main__":
    which = sys.argv[1:] or list(SCEN)
    conn, _, _ = fresh()
    odb.reset_schema(conn)                 # this stream's own 'emlab' database only (port asserted in oldenv)
    out = {"old_head": oldenv.OLD_HEAD}
    for n in which:
        try:
            out[n] = SCEN[n]()
        except Exception as e:  # noqa: BLE001
            out[n] = {"property_holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print(n, json.dumps(out[n], default=str))
    write_result("old_code_N1_N7", out)
