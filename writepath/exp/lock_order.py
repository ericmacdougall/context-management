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
usage: python -m exp.lock_order [R_MM R_MK R_HP R_HR R_DA R_CTL_MM R_CTL_DA]   (default 10 5 5 5 5 10 5)"""
import json
import sys
import threading
import time
import uuid

from exp import mutual_mentions as MX
from exp.common import write_result
from exp.patches import variant, patched
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


def main(r_mm, r_mk, r_hp, r_hr, r_da, r_cmm, r_cda):
    out = {"cases": {"MM": _arm(mm_once, r_mm), "MK": _arm(mk_once, r_mk), "HP": _arm(hp_once, r_hp),
                     "HR": _arm(hr_once, r_hr), "DA": _arm(da_once, r_da)}}
    out["controls"] = {"MM_prefix_hold_check": _control("wp_hold_check", HOLD_FIXED, HOLD_PRE, lambda: mm_once(do_sweep=False), r_cmm),
                       "DA_prefix_declare": _control("wp_identifier_declare", DECL_FIXED, DECL_PRE, da_once, r_cda)}
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
    print("lock_order", out["verdict"], json.dumps(summary), flush=True)
    write_result("lock_order", out)


if __name__ == "__main__":
    d = [10, 5, 5, 5, 5, 10, 5]
    a = [int(x) for x in sys.argv[1:8]]
    main(*(a + d[len(a):]))
