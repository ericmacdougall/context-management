"""Top-30 row 1: a zombie session run. Run A stalls (named hook) past its lease; run B takes the lease (epoch+1); A
resumes, appends 20 events and claims/confirms an inbox item. The inbox client used here is deliberately NAIVE (no epoch
predicate of its own) in both arms, so only the store can stop the zombie.
Arms: fenced = wp_events; control = wp_ctl (same schema without the fence triggers).
Stall points: 'before_txn' (stalled after preparing ciphertext) and 'in_txn' (stalled inside the append transaction
holding the session chain row). usage: python -m exp.row1_zombie [R]"""
import json
import sys
import threading
import time

from exp.common import write_result
from exp.util import tenant, msg, es_count_all
from wp import db, keys as K, lease as L, log, projector
from wp.es import ES
from wp.setup import CTL_DB


def naive_claim(conn, lease, item):
    return log._fenced(conn, "update inbox set state='claimed', claimed_epoch=%s, shown_count=shown_count+1, lease_scope=%s, lease_epoch=%s "
                             "where tenant_id=%s and item_id=%s and state in ('new','claimed') returning 1", (lease.epoch, lease.scope, lease.epoch, lease.tenant, item))


def naive_confirm(conn, lease, item):
    return log._fenced(conn, "update inbox set state='confirmed', confirmed_epoch=%s, lease_scope=%s, lease_epoch=%s "
                             "where tenant_id=%s and item_id=%s and state='claimed' returning 1", (lease.epoch, lease.scope, lease.epoch, lease.tenant, item))


def one(arm, stall_at):
    dbname = CTL_DB if arm == "control" else None
    mk = (lambda: db.connect(db=dbname)) if dbname else db.connect
    cA, cB, cM = mk(), mk(), mk()
    ksA, ksB, es = K.KeyService(), K.KeyService(), ES()
    t = tenant("r1")
    clock = lambda: cM.execute("select clock_timestamp()").fetchone()[0]  # noqa: E731
    A = L.acquire(cA, t, L.session_scope("s1"), ttl_s=1.0)
    log.append(cA, ksA, A, [msg(i, f"good event {i}", "U") for i in range(5)])
    log.inbox_enqueue(cA, ksA, A, "i1", "s1", "U", {"text": "answer to your ask"})
    stalled, resume, w = threading.Event(), threading.Event(), {"zombie_results": [], "zombie_inbox": []}

    def hook(name, data=None):
        if name == stall_at and not stalled.is_set():
            w["t_stall"] = clock()
            stalled.set()
            resume.wait(30)

    def zombie():
        r = log.append(cA, ksA, A, [msg(100, "zqzombie event 0", "U")], hook=hook)
        w["t_resume"] = w.get("t_resume") or clock()
        w["zombie_results"].append(r)
        for i in range(1, 20):
            w["zombie_results"].append(log.append(cA, ksA, A, [msg(100 + i, f"zqzombie event {i}", "U")]))
        for f in (naive_claim, naive_confirm):
            try:
                w["zombie_inbox"].append((f.__name__, bool(f(cA, A, "i1"))))
            except log.StaleWrite:
                w["zombie_inbox"].append((f.__name__, "refused"))
    th = threading.Thread(target=zombie)
    th.start()
    stalled.wait(30)
    B = None
    while B is None:                                     # wait for A's lease to expire on the DATABASE clock
        B = L.acquire(cB, t, L.session_scope("s1"), ttl_s=30.0, lock_timeout_ms=2000)
        if B is None:
            time.sleep(0.05)
    w["t_B"] = B.acquired_at
    w["B_claim"] = bool(naive_claim(cB, B, "i1"))
    w["t_resume"] = clock()
    resume.set()
    log.append(cB, ksB, B, [msg(200 + i, f"B event {i}", "U") for i in range(3)])
    th.join(60)
    try:
        w["B_confirm"] = bool(naive_confirm(cB, B, "i1"))
    except log.StaleWrite:
        w["B_confirm"] = "refused"
    accepted = sum(len(r["accepted"]) for r in w["zombie_results"])
    quarantined = sum(r["quarantined"] for r in w["zombie_results"])
    ok_chain, n_chain, epochs = log.verify_chain(cA, t, "s1")
    jobs_zombie = cA.execute("select count(*) from analyst_jobs where tenant_id=%s and lease_epoch=%s", (t, A.epoch)).fetchone()[0] - 5
    shown, conf_epoch = cA.execute("select shown_count, confirmed_epoch from inbox where tenant_id=%s and item_id='i1'", (t,)).fetchone()
    late_seq = cA.execute("select count(*) from events where tenant_id=%s and lease_epoch=%s and seq > 5", (t, A.epoch)).fetchone()[0]
    es_zombie = None
    if arm == "fenced":
        p = projector.Projector(db.connect(), K.KeyService(), es, t)
        p.run(); p.stop()
        es_zombie = es_count_all(es, t, {"match_phrase": {"content": "zqzombie"}})
    holds = (accepted == 0 and quarantined == 20 and ok_chain and jobs_zombie == 0 and shown == 1 and conf_epoch == B.epoch and late_seq == 0
             and (es_zombie in (0, None)))
    why = [] if holds else [f"accepted={accepted}", f"shown={shown}", f"confirmed_epoch={conf_epoch}", f"zombie_jobs={jobs_zombie}"]
    return holds, " ".join(why), {
        "zombie_attempts": len(w["zombie_results"]), "accepted": accepted, "quarantined": quarantined, "chain_ok": ok_chain, "chain_len": n_chain,
        "chain_epochs": epochs, "zombie_events_in_chain_after_takeover": late_seq, "zombie_analyst_jobs": jobs_zombie, "inbox_shown": shown,
        "inbox_confirmed_epoch": conf_epoch, "B_epoch": B.epoch, "zombie_inbox": w["zombie_inbox"], "B_confirm": w["B_confirm"], "es_zombie_docs": es_zombie,
        "overlap": str(A.acquired_at) + " < " + str(w["t_stall"]) + " < " + str(w["t_B"]) + " < " + str(w["t_resume"]),
        "overlap_ok": A.acquired_at < w["t_stall"] < w["t_B"] < w["t_resume"]}


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = {}
    for stall_at in ("before_txn", "in_txn"):
        for arm in ("fenced", "control"):
            runs = []
            for _ in range(R):
                try:
                    h, why, wit = one(arm, stall_at)
                except Exception as e:  # noqa: BLE001
                    h, why, wit = None, f"ERROR {type(e).__name__}: {str(e)[:200]}", {}
                runs.append({"holds": h, "why": why, "witness": wit})
            out[f"{stall_at}/{arm}"] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                                        "errors": sum(r["holds"] is None for r in runs), "runs": runs}
            print(stall_at, arm, {k: v for k, v in out[f"{stall_at}/{arm}"].items() if k != "runs"}, json.dumps(runs[0], default=str)[:600])
    out["pass"] = all(out[f"{s}/fenced"]["holds"] == R and out[f"{s}/control"]["violated"] >= int(0.9 * R) for s in ("before_txn", "in_txn"))
    print("PASS" if out["pass"] else "FAIL")
    write_result("row1_zombie", out)
