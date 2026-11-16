"""DESIGN 3.1.1 (THRESHOLDS.md section H): per-session sequence and hash chain, owner memsys at a13b524 unmodified.
Workload, tamper steps, control verifier and checks are this stream's own. Every tamper runs inside a transaction that
is rolled back after the verdict, so each runs against the same clean 2,000-event chain.

usage (systems venv): python chain_test.py
"""
import datetime as dt
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysdrv  # noqa: E402
from memsys import chain, db, kms, store  # noqa: E402

T, S = "rep", "chain"
WRITERS, PER = 8, 250
TS = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)


def ev(text, i):
    return dict(ts_event=TS + dt.timedelta(microseconds=i), actor="user", trust_origin="user_typed", kind="message",
                subject_id="u1", content=text)


def writer(p, q):
    conn = db.connect(autocommit=True)
    waits, t_first, t_last = [], None, None
    for i in range(PER):
        t = time.time()
        t_first = t_first or t
        ids, w = store.append_events(conn, T, S, [ev("w%d-%d" % (p, i), p * PER + i)])
        waits.append(w)
        t_last = time.time()
    q.put((p, waits, t_first, t_last))


def verify(conn):
    return chain.verify_chain(store.session_rows(conn, T, S))


def verify_no_hash(conn):
    """CONTROL verifier: seq continuity and prev_hash linkage to the previous row's STORED hash; hash not recomputed."""
    prev, exp, n = chain.GENESIS, 1, 0
    for r in store.session_rows(conn, T, S):
        n += 1
        if r["seq"] != exp or r["prev_hash"] != prev:
            return False, r["seq"], n
        prev, exp = r["hash"], exp + 1
    return True, None, n


def tamper(name, sqls, expect_bad_seq, results, disable_trigger=True):
    c = db.connect(autocommit=False)
    rec = {"name": name}
    try:
        if disable_trigger:
            c.execute("alter table events disable trigger events_no_update")
            rec["app_role_could_disable_trigger"] = True
        for sql, args in sqls:
            c.execute(sql, args)
        ok, bad, n = verify(c)
        ok2, bad2, _ = verify_no_hash(c)
        rec.update({"verify_ok": ok, "first_bad_seq": bad, "rows": n, "expected_bad_seq": expect_bad_seq,
                    "detected_as_expected": (not ok) and bad == expect_bad_seq,
                    "control_no_hash_verify_ok": ok2, "control_first_bad_seq": bad2})
    except Exception as e:  # a refusal by the database is a result, not an error
        rec.update({"refused_by_database": True, "error": type(e).__name__, "message": str(e).splitlines()[0][:200]})
    finally:
        c.rollback()
        c.close()
    results.append(rec)
    print(rec, flush=True)


def main():
    conn, lock, es = sysdrv.conns()
    db.reset_schema(conn)
    t0 = time.time()
    q = mp.Queue()
    ps = [mp.Process(target=writer, args=(p, q)) for p in range(WRITERS)]
    for p in ps:
        p.start()
    got = [q.get() for _ in ps]
    for p in ps:
        p.join()
    wall = time.time() - t0
    waits = sorted(w for _, ws, _, _ in got for w in ws)
    starts, ends = [g[2] for g in got], [g[3] for g in got]
    overlap_s = min(ends) - max(starts)
    ok, bad, n = verify(conn)
    rows = list(store.session_rows(conn, T, S))
    seqs = [r["seq"] for r in rows]
    plain = kms.get().dec_many(T, [("u1", r["content_enc"]) for r in rows])
    concurrency = {"writers": WRITERS, "appends_each": PER, "rows": n, "verify_ok": ok, "first_bad_seq": bad,
                   "gapless_1_to_n": seqs == list(range(1, len(seqs) + 1)), "distinct_payloads": len(set(plain)),
                   "expected_payloads": WRITERS * PER, "wall_s": round(wall, 2),
                   "overlap_witness": {"all_writers_active_s": round(overlap_s, 2),
                                       "appends_with_lock_wait_gt_1ms": sum(1 for w in waits if w > 0.001),
                                       "lock_wait_p50_ms": round(1000 * waits[len(waits) // 2], 2),
                                       "lock_wait_max_ms": round(1000 * waits[-1], 2)}}
    print(concurrency, flush=True)
    # tamper tests
    res = []
    # t0: plain UPDATE by the app role with triggers on
    tamper("t0_update_with_trigger_on", [("update events set content_enc = content_enc || '\\x00'::bytea where tenant_id=%s and session_id=%s and seq=700", (T, S))],
           700, res, disable_trigger=False)
    tamper("t1_update_content_seq700", [("update events set content_enc = content_enc || '\\x00'::bytea where tenant_id=%s and session_id=%s and seq=700", (T, S))], 700, res)
    tamper("t2_delete_seq900", [("delete from events where tenant_id=%s and session_id=%s and seq=900", (T, S))], 901, res)
    tamper("t3_swap_content_1000_1001", [
        ("update events e set content_enc = o.content_enc from events o where e.tenant_id=%s and e.session_id=%s and o.tenant_id=e.tenant_id and o.session_id=e.session_id and ((e.seq=1000 and o.seq=1001) or (e.seq=1001 and o.seq=1000))", (T, S))], 1000, res)
    last = rows[-1]
    dup = ("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) "
           "values (%s,%s,%s,%s,%s,'u1','user','user_typed','message',%s,%s,%s)")
    tamper("t4_duplicate_seq_insert", [(dup, (T, S, 1500, "dup-1500", TS, b"x", "0" * 64, "0" * 64))], None, res, disable_trigger=False)
    gap_seq = len(rows) + 5
    h = chain.event_hash(last["hash"], T, S, gap_seq, "%s/%s/%d" % (T, S, gap_seq), db.iso(TS), "user", "user_typed", "message", "u1", b"forged")
    tamper("t5_insert_gap_at_end", [(dup, (T, S, gap_seq, "%s/%s/%d" % (T, S, gap_seq), TS, b"forged", last["hash"], h))], gap_seq, res, disable_trigger=False)
    # limit (not a gate): a writer with database access appends a correctly chained forged event
    nxt = len(rows) + 1
    h2 = chain.event_hash(last["hash"], T, S, nxt, "%s/%s/%d" % (T, S, nxt), db.iso(TS), "user", "user_typed", "message", "u1", b"forged")
    tamper("limit_forged_but_correctly_chained_append", [(dup, (T, S, nxt, "%s/%s/%d" % (T, S, nxt), TS, b"forged", last["hash"], h2))], None, res, disable_trigger=False)
    by = {r["name"]: r for r in res}
    checks = {
        "concurrency_chain_ok_gapless_distinct": ok and concurrency["gapless_1_to_n"] and concurrency["distinct_payloads"] == WRITERS * PER,
        "overlap_witness_nonzero": concurrency["overlap_witness"]["appends_with_lock_wait_gt_1ms"] > 0,
        "t0_update_refused_by_trigger": by["t0_update_with_trigger_on"].get("refused_by_database", False),
        "t1_detected": by["t1_update_content_seq700"].get("detected_as_expected", False),
        "t2_detected": by["t2_delete_seq900"].get("detected_as_expected", False),
        "t3_detected": by["t3_swap_content_1000_1001"].get("detected_as_expected", False),
        "t4_refused": by["t4_duplicate_seq_insert"].get("refused_by_database", False),
        "t5_detected": by["t5_insert_gap_at_end"].get("detected_as_expected", False),
        "control_no_hash_verifier_passes_t1": by["t1_update_content_seq700"].get("control_no_hash_verify_ok") is True,
        "limit_forged_chained_append_passes_verify": by["limit_forged_but_correctly_chained_append"].get("verify_ok") is True,
        "app_role_can_disable_append_only_trigger": by["t1_update_content_seq700"].get("app_role_could_disable_trigger", False)}
    out = {"stamp": sysdrv.stamp(__file__, {"tenant": T, "session": S, "writers": WRITERS, "per_writer": PER}, conn),
           "concurrency": concurrency, "tamper": res, "threshold_checks": checks,
           "verdict_gate": all(v for k, v in checks.items() if not k.startswith(("limit_", "app_role_")))}
    p = sysdrv.save("chain_3_1_1.json", out)
    print(checks, "saved", p)


if __name__ == "__main__":
    main()
