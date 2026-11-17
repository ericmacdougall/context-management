"""Lock order of erasure, legal holds and identifier declaration (THRESHOLDS amendments 17 and 17a). Deterministic:
every interleaving is forced by holding a transaction open and waiting until the next statement is observed waiting
on a lock (pg_stat_activity, wait_event_type Lock, read on the admin connection), never by sleeping and hoping.

  MM  two marked erasures of subjects who mention each other (A1 by A mentions B, B1 by B mentions A). T1 runs
      wp_hold_check(A) and T2 wp_hold_check(B) (the first statement of a cascade batch), then T1 runs one
      wp_erasure_cascade batch of A (waits or finishes), then T2 one batch of B. Holds iff both batches commit.
      Afterwards erasure.sweep finishes both jobs (witness: a job that lost a batch is left retryable).
  MK  T1 holds one open cascade batch of A; T2 marks B. Holds iff T2 waited for T1 and committed after it.
  HP  T1 holds one open cascade batch of A; the legal role places a hold on B. Holds iff the placement waited for T1,
      then succeeded, and wp_hold_check(B) afterwards raises WPS16.
  HR  a hold on B; T1 holds one open cascade batch of A; the legal role releases the hold. Holds iff the release
      waited for T1, then succeeded.
  DA  T0 share-locks the tenant's identifier_epochs row; T1 runs wp_identifier_declare(Y) (observed waiting); T2
      appends an event by X that mentions Y (observed waiting, or finished); T0 commits. Holds iff T1 and T2 both
      succeed and the event really mentions Y (otherwise the run is vacuous and scored an error).
Controls (one patch each, reverted): MM with the pre-fix wp_hold_check (subject FOR UPDATE); DA with the pre-fix
wp_identifier_declare (subject FOR UPDATE). Each must deadlock in >= R-1 of R runs. At the pre-fix code the controls
cannot be cut (the fixed fragment is absent) and are recorded as unavailable; that run is failing-first evidence.
A deadlock is sqlstate 40P01 on a test connection; the database's deadlock counter delta is a second witness.
Amendment 17b, at the fixed code only: M2 (gating) B's mark behind two share holders at once (an open declare of B and
an open wp_hold_check(B)); MS (measurement, not gating) a relay of share holders against B's waiting mark, recording
whether the mark is starved until its lock_timeout and whether a later sweep still finishes the job.
usage: python -m exp.lock_order [KEY=N ...]   keys and defaults: COUNTS (amendment 17b)"""
import json
import sys
import threading
import time
import uuid

from exp import mutual_mentions as MX
from exp.common import write_result
from exp.patches import function_sql, variant, patched
from exp.util import tenant, msg, session, env
from wp import db, erasure, log, mentions as M

DEADLOCK = "40P01"
WAIT_S = 2.0          # holders stay idle far under the role's 5 s idle_in_transaction_session_timeout
BATCH = 500
HOLD_FIXED = "perform 1 from subjects where tenant_id = t and subject_id = s for share;"
HOLD_PRE = "perform 1 from subjects where tenant_id = t and subject_id = s for update;"
DECL_FIXED = "select erased into er from subjects where tenant_id = t and subject_id = s for share;"
DECL_PRE = "select erased into er from subjects where tenant_id = t and subject_id = s for update;"


class Bg(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn, self.res, self.err, self.end = fn, None, None, None

    def run(self):
        try:
            self.res = self.fn()
        except Exception as e:  # noqa: BLE001
            self.err = e
        finally:
            self.end = time.monotonic()


def _st(e):
    return None if e is None else (getattr(e, "sqlstate", None) or type(e).__name__)


def _desc(e):
    return None if e is None else f"{_st(e)} {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}"


def _waiting(adm, pid, th=None, timeout=WAIT_S):
    """True once backend `pid` waits on a lock; False if the thread finished first or the timeout passed."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if th is not None and not th.is_alive():
            return False
        r = adm.execute("select wait_event_type from pg_stat_activity where pid = %s", (pid,)).fetchone()
        if r and r[0] == "Lock":
            return True
        time.sleep(0.02)
    return False


def _deadlocks(adm):
    adm.execute("select pg_stat_clear_snapshot()")
    return adm.execute("select deadlocks from pg_stat_database where datname = current_database()").fetchone()[0]


def _finish(c, th):
    """Commit a transaction whose statement succeeded, roll back one that failed; returns the commit error or None."""
    try:
        if th.err is None:
            c.commit()
            return None
        c.rollback()
        return None
    except Exception as e:  # noqa: BLE001
        return e


def _mark(conn, t, s):
    jid = erasure.request(conn, t, s)
    ce = erasure._claim(conn, jid, 60)
    conn.execute("select wp_erasure_mark(%s)", (jid,))
    erasure._release(conn, jid, ce)            # claim expired at once: the job is free for a later sweep
    return jid


def _tx(name):
    return db.connect(autocommit=False, application_name=f"lo-{name}")


def _scored(w, errors, deadlocked, ok):
    w["errors"] = [x for x in errors if x]
    w["deadlock"] = deadlocked
    w["holds"] = False if deadlocked else (None if w["errors"] else bool(ok))
    return w


def mm_once(do_sweep=True):
    t, ids, _c1 = MX.build()
    conn, ks, es = env()
    jobs = {s: _mark(conn, t, s) for s in ("A", "B")}
    adm = db.admin()
    d0 = _deadlocks(adm)
    c1, c2 = _tx("T1"), _tx("T2")
    w = {"tenant": t}
    try:
        c1.execute("select wp_hold_check(%s, %s)", (t, "A"))
        c2.execute("select wp_hold_check(%s, %s)", (t, "B"))
        th1 = Bg(lambda: c1.execute("select wp_erasure_cascade(%s, %s)", (jobs["A"], BATCH)).fetchone()[0])
        th1.start()
        w["t1_waited"] = _waiting(adm, c1.info.backend_pid, th1)
        th2 = Bg(lambda: c2.execute("select wp_erasure_cascade(%s, %s)", (jobs["B"], BATCH)).fetchone()[0])
        th2.start()
        th1.join(30)
        th2.join(30)
        e1, e2 = _finish(c1, th1), _finish(c2, th2)
    finally:
        c1.close()
        c2.close()
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    w["T1"], w["T2"] = {"rows": th1.res, "err": _desc(th1.err)}, {"rows": th2.res, "err": _desc(th2.err)}
    dl = DEADLOCK in (_st(th1.err), _st(th2.err))
    other = [_desc(x) for x in (th1.err, th2.err) if x is not None and _st(x) != DEADLOCK] + [_desc(e1), _desc(e2)]
    if th1.is_alive() or th2.is_alive():
        other.append("a batch did not finish in 30 s")
    _scored(w, other, dl, th1.err is None and th2.err is None)
    if do_sweep:                               # retryability witness: both jobs must still be finishable
        w["sweep"] = erasure.sweep(conn, ks, es, tenant=t, physical="followup")
        w["after_sweep"] = {s: st for s, st in adm.execute("select subject_id, state || coalesce('/' || last_error_type, '') from erasure_jobs "
                                                            "where tenant_id = %s order by subject_id", (t,)).fetchall()}
    adm.close()
    return w


def _open_batch_of_A():
    t, ids, _c1 = MX.build()
    conn, ks, es = env()
    ja = _mark(conn, t, "A")
    return t, conn, ja


def mk_once():
    t, conn, ja = _open_batch_of_A()
    jb = erasure.request(conn, t, "B")
    ceb = erasure._claim(conn, jb, 60)
    adm = db.admin()
    d0 = _deadlocks(adm)
    c1, c2 = _tx("T1"), db.connect(application_name="lo-T2")
    w = {"tenant": t}
    try:
        w["t1_rows"] = c1.execute("select wp_erasure_cascade(%s, %s)", (ja, BATCH)).fetchone()[0]
        th = Bg(lambda: c2.execute("select wp_erasure_mark(%s)", (jb,)))
        th.start()
        w["t2_waited"] = _waiting(adm, c2.info.backend_pid, th)
        tc = time.monotonic()
        c1.commit()
        th.join(30)
    finally:
        c1.close()
        c2.close()
    erasure._release(conn, jb, ceb)
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    w["T2"] = {"err": _desc(th.err), "after_t1_commit": bool(th.end and th.end > tc)}
    w["b_state"] = adm.execute("select state from erasure_jobs where job_id = %s", (jb,)).fetchone()[0]
    adm.close()
    other = [_desc(th.err)] if th.err is not None and _st(th.err) != DEADLOCK else []
    return _scored(w, other, _st(th.err) == DEADLOCK,
                   th.err is None and w["t2_waited"] and w["T2"]["after_t1_commit"] and w["b_state"] == "marked")


def hp_once():
    t, conn, ja = _open_batch_of_A()
    adm, lg = db.admin(), db.legal()
    d0 = _deadlocks(adm)
    c1 = _tx("T1")
    hid = "lo" + uuid.uuid4().hex[:10]
    w = {"tenant": t}
    try:
        w["t1_rows"] = c1.execute("select wp_erasure_cascade(%s, %s)", (ja, BATCH)).fetchone()[0]
        th = Bg(lambda: erasure.place_hold(lg, t, "B", hid, "lock_order_test"))
        th.start()
        w["place_waited"] = _waiting(adm, lg.info.backend_pid, th)
        tc = time.monotonic()
        c1.commit()
        th.join(30)
    finally:
        c1.close()
    w["place"] = {"err": _desc(th.err), "after_t1_commit": bool(th.end and th.end > tc)}
    chk = None
    try:
        conn.execute("select wp_hold_check(%s, %s)", (t, "B"))
    except Exception as e:  # noqa: BLE001
        chk = e
    w["hold_check_B"] = _st(chk)
    if th.err is None:
        lg.execute("select wp_hold_release(%s)", (hid,))
    lg.close()
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    adm.close()
    other = [_desc(th.err)] if th.err is not None and _st(th.err) != DEADLOCK else []
    return _scored(w, other, _st(th.err) == DEADLOCK,
                   th.err is None and w["place_waited"] and w["place"]["after_t1_commit"] and _st(chk) == "WPS16")


def hr_once():
    t, ids, _c1 = MX.build()
    conn, ks, es = env()
    adm, lg = db.admin(), db.legal()
    hid = "lo" + uuid.uuid4().hex[:10]
    erasure.place_hold(lg, t, "B", hid, "lock_order_test")
    ja = _mark(conn, t, "A")
    d0 = _deadlocks(adm)
    c1 = _tx("T1")
    w = {"tenant": t}
    try:
        w["t1_rows"] = c1.execute("select wp_erasure_cascade(%s, %s)", (ja, BATCH)).fetchone()[0]
        th = Bg(lambda: lg.execute("select wp_hold_release(%s)", (hid,)))
        th.start()
        w["release_waited"] = _waiting(adm, lg.info.backend_pid, th)
        tc = time.monotonic()
        c1.commit()
        th.join(30)
    finally:
        c1.close()
    lg.close()
    w["release"] = {"err": _desc(th.err), "after_t1_commit": bool(th.end and th.end > tc)}
    w["released"] = bool(adm.execute("select released_at is not null from legal_holds where hold_id = %s", (hid,)).fetchone()[0])
    w["withheld_A"] = adm.execute("select count(*) from erasure_withheld where job_id = %s", (ja,)).fetchone()[0]
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    adm.close()
    other = [_desc(th.err)] if th.err is not None and _st(th.err) != DEADLOCK else []
    return _scored(w, other, _st(th.err) == DEADLOCK,
                   th.err is None and w["release_waited"] and w["release"]["after_t1_commit"] and w["released"])


def da_once():
    t = tenant("lo")
    conn, ks, es = env()
    M.declare(conn, ks, t, "Y", ["ypsilanti"])
    c1, ks1, _ = env()
    c2, ks2, _ = env()
    lease = session(c2, t)
    adm = db.admin()
    d0 = _deadlocks(adm)
    t0 = db.admin(autocommit=False)
    w = {"tenant": t}
    t0.execute("select 1 from identifier_epochs where tenant_id = %s for share", (t,))
    th1 = Bg(lambda: M.declare(c1, ks1, t, "Y", ["ypsilanti nova"]))
    th1.start()
    w["t1_waited"] = _waiting(adm, c1.info.backend_pid, th1)
    th2 = Bg(lambda: log.append(c2, ks2, lease, [msg(0, "we met ypsilanti at the station zqlo", "X")]))
    th2.start()
    w["t2_waited"] = _waiting(adm, c2.info.backend_pid, th2)
    t0.commit()
    t0.close()
    th1.join(30)
    th2.join(30)
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    w["T1"], w["T2"] = {"err": _desc(th1.err)}, {"err": _desc(th2.err)}
    acc = (th2.res or {}).get("accepted") or []
    w["t2_mentions_Y"] = bool(acc) and bool(adm.execute("select mentions @> array['Y'] from events where tenant_id = %s and event_id = %s",
                                                        (t, acc[0])).fetchone()[0])
    adm.close()
    for c in (c1, c2):
        c.close()
    dl = DEADLOCK in (_st(th1.err), _st(th2.err))
    other = [_desc(x) for x in (th1.err, th2.err) if x is not None and _st(x) != DEADLOCK]
    if th1.is_alive() or th2.is_alive():
        other.append("a transaction did not finish in 30 s")
    if not dl and th2.err is None and not w["t2_mentions_Y"]:
        other.append("vacuous: the appended event does not mention Y")
    return _scored(w, other, dl, th1.err is None and th2.err is None)


def m2_once():
    """Amendment 17b M2: B's mark behind two share holders at once (an open declare of B and an open hold check)."""
    t, ids, _c1 = MX.build()
    conn, ks, es = env()
    jb = erasure.request(conn, t, "B")
    ceb = erasure._claim(conn, jb, 60)
    adm = db.admin()
    d0 = _deadlocks(adm)
    h1, h2, cm = _tx("H1"), _tx("H2"), db.connect(application_name="lo-mark")
    w = {"tenant": t}
    try:
        seq = M.declare(h1, ks, t, "B", ["bartholomew senior"])   # open: B FOR SHARE, identifier_epochs FOR UPDATE
        h2.execute("select wp_hold_check(%s, %s)", (t, "B"))       # open: B FOR SHARE
        th = Bg(lambda: cm.execute("select wp_erasure_mark(%s)", (jb,)))
        th.start()
        w["mark_waited"] = _waiting(adm, cm.info.backend_pid, th)
        h1.commit()
        time.sleep(0.3)
        w["waiting_after_first_commit"] = th.is_alive() and _waiting(adm, cm.info.backend_pid, th, timeout=0.5)
        tc = time.monotonic()
        h2.commit()
        th.join(30)
    finally:
        for c in (h1, h2, cm):
            c.close()
    erasure._release(conn, jb, ceb)
    st, xmax = adm.execute("select state, ident_xmax from erasure_jobs where job_id = %s", (jb,)).fetchone()
    time.sleep(1.5)
    w["db_deadlocks_delta"] = _deadlocks(adm) - d0
    adm.close()
    w.update(declared_seq=seq, b_state=st, ident_xmax=xmax, mark={"err": _desc(th.err), "after_second_commit": bool(th.end and th.end > tc)})
    other = [_desc(th.err)] if th.err is not None and _st(th.err) != DEADLOCK else []
    return _scored(w, other, _st(th.err) == DEADLOCK,
                   th.err is None and w["mark_waited"] and w["waiting_after_first_commit"] and w["mark"]["after_second_commit"]
                   and st == "marked" and xmax is not None and xmax >= seq)


def ms_once(period_s=0.8, span_s=12.0):
    """Amendment 17b MS (measurement): a relay of share holders on B while B's mark waits for FOR UPDATE."""
    t, ids, _c1 = MX.build()
    conn, ks, es = env()
    jb = erasure.request(conn, t, "B")
    ceb = erasure._claim(conn, jb, 60)
    adm = db.admin()
    w = {"tenant": t}
    n = [0]

    def holder():
        c = _tx(f"S{n[0]}")
        n[0] += 1
        th = Bg(lambda: c.execute("select wp_hold_check(%s, %s)", (t, "B")))
        th.start()
        th.join(0.3)
        return c, th

    prev, th0 = holder()
    cm = db.connect(application_name="lo-mark")
    thm = Bg(lambda: cm.execute("select wp_erasure_mark(%s)", (jb,)))
    tm0 = time.monotonic()
    thm.start()
    w["mark_waited"] = _waiting(adm, cm.info.backend_pid, thm)
    granted = queued = 0
    errs = [_desc(th0.err)] if th0.err else []
    while thm.is_alive() and time.monotonic() - tm0 < span_s:
        time.sleep(max(0.0, period_s - 0.3))
        c, th = holder()
        if th.is_alive():                      # the new share lock queued behind the waiting FOR UPDATE
            queued += 1
            prev.commit()
            prev.close()
            prev = None
            th.join(30)
            (c.commit if th.err is None else c.rollback)()
            c.close()
            break
        if th.err:
            errs.append(_desc(th.err))
        granted += 1
        prev.commit()
        prev.close()
        prev = c
    if prev is not None:
        prev.commit()
        prev.close()
    thm.join(30)
    cm.close()
    erasure._release(conn, jb, ceb)
    w.update(share_granted_while_mark_waited=granted, share_queued_behind_mark=queued, mark_err=_desc(thm.err),
             mark_wait_s=round((thm.end or time.monotonic()) - tm0, 2), starved=_st(thm.err) == "55P03",
             state_after=adm.execute("select state from erasure_jobs where job_id = %s", (jb,)).fetchone()[0])
    w["sweep"] = erasure.sweep(conn, ks, es, tenant=t, physical="followup")
    w["state_after_sweep"] = adm.execute("select state || coalesce('/' || last_error_type, '') from erasure_jobs where job_id = %s",
                                         (jb,)).fetchone()[0]
    adm.close()
    if thm.err is not None and _st(thm.err) not in ("55P03",):
        errs.append(_desc(thm.err))
    return _scored(w, errs, _st(thm.err) == DEADLOCK, True)


def _arm(fn, r):
    runs = []
    for _ in range(r):
        try:
            w = fn()
        except Exception as ex:  # noqa: BLE001
            w = {"holds": None, "deadlock": False, "errors": [f"harness {type(ex).__name__}: {str(ex)[:160]}"]}
        runs.append(w)
    return {"R": r, "holds": sum(x["holds"] is True for x in runs), "deadlocked": sum(bool(x.get("deadlock")) for x in runs),
            "errors": sum(x["holds"] is None for x in runs), "runs": runs}


def _control(fname, fixed, pre, fn, r):
    try:
        apply_revert = variant(fname, fixed, pre)
    except AssertionError:
        return {"R": r, "available": False, "why": f"{fname}: fixed fragment absent (pre-fix code)"}
    with patched(*apply_revert):
        out = _arm(fn, r)
    out.update(available=True, patch=f"{fname}: '{fixed}' -> '{pre}'")
    return out


COUNTS = {"MM": 30, "MK": 5, "HP": 5, "HR": 5, "DA": 30, "M2": 10, "MS": 3, "CTL_MM": 10, "CTL_DA": 10}   # amendment 17b


def main(n):
    out = {"counts": n, "cases": {"MM": _arm(mm_once, n["MM"]), "MK": _arm(mk_once, n["MK"]), "HP": _arm(hp_once, n["HP"]),
                                  "HR": _arm(hr_once, n["HR"]), "DA": _arm(da_once, n["DA"])}}
    if HOLD_FIXED in function_sql("wp_hold_check"):                               # M2 needs two concurrent share holders: impossible at the pre-fix code
        out["cases"]["M2"] = _arm(m2_once, n["M2"])
        out["measurements"] = {"MS": _arm(ms_once, n["MS"])}
    out["controls"] = {"MM_prefix_hold_check": _control("wp_hold_check", HOLD_FIXED, HOLD_PRE, lambda: mm_once(do_sweep=False), n["CTL_MM"]),
                       "DA_prefix_declare": _control("wp_identifier_declare", DECL_FIXED, DECL_PRE, da_once, n["CTL_DA"])}
    why = []
    for k, a in out["cases"].items():
        if a["holds"] != a["R"]:
            why.append(f"{k}: holds {a['holds']}/{a['R']}, deadlocked {a['deadlocked']}, errors {a['errors']}")
    for k, c in out["controls"].items():
        if not c.get("available"):
            why.append(f"control {k} unavailable: {c['why']}")
        elif c["deadlocked"] < c["R"] - 1:
            why.append(f"control {k}: deadlocked {c['deadlocked']}/{c['R']} < {c['R'] - 1} (reproducer not shown sensitive)")
    out["verdict"], out["why"] = ("FAIL" if why else "PASS"), why
    summary = {k: {x: a[x] for x in ("R", "holds", "deadlocked", "errors")} for k, a in out["cases"].items()}
    summary.update({k: ({x: c[x] for x in ("R", "deadlocked", "errors")} if c.get("available") else "unavailable")
                    for k, c in out["controls"].items()})
    if "measurements" in out:
        summary["MS"] = [{x: r.get(x) for x in ("share_granted_while_mark_waited", "share_queued_behind_mark", "starved",
                                                "mark_wait_s", "state_after_sweep")} for r in out["measurements"]["MS"]["runs"]]
    print("lock_order", out["verdict"], json.dumps(summary), flush=True)
    write_result("lock_order", out)


if __name__ == "__main__":
    counts = dict(COUNTS)
    for a in sys.argv[1:]:
        k, v = a.split("=", 1)
        if k not in counts:
            raise SystemExit(f"unknown count {k}; known {sorted(counts)}")
        counts[k] = int(v)
    main(counts)
