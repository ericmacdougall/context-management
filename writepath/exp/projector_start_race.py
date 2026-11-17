"""A projector that loses the start-up race to a forced takeover (THRESHOLDS amendment 19).

Projector.step() starts the projector when it holds no lease. start() takes the lease (epoch e), moves the ES write
alias to w<e> (families.fence_es) and inserts the cursor rows under epoch e. A forced takeover (epoch e+1) that lands
inside start() makes fence_es raise StaleEpoch (alias already at w<e+1>) or the cursor insert raise StaleWrite (fence
trigger). step()'s docstring promises "on a lost lease or refused write, drops the lease and returns -1"; at fd4ed53
the start() call sat outside step()'s handler, so the loser's process died with the exception (11 of 11 child
exceptions in the fd4ed53 e2e E1 run came from there).

Deterministic, one process, two connections. The takeover is injected by wrapping wp.families.fence_es:
  A  takeover between acquire and fence_es  (P1's fence_es then sees w<e+1>: StaleEpoch)
  B  takeover between fence_es and the cursor insert  (P1's insert is fenced: StaleWrite)
  C  every start of P1 loses (a fresh takeover before each of its fence_es calls); P1.run(max_rounds=5)
Property: A, B: P1.step() returns -1 without raising, P1 holds no lease, the lease row's holder is the taker, and the
taker then projects every event of the tenant exactly once. C: P1.run(5) returns (no exception) after 5 starts.
Control (one mechanism): Projector.step replaced by the fd4ed53 body (start() outside the handler), verbatim.
usage: python -m exp.projector_start_race [R]"""
import sys

from exp.common import write_result
from exp.util import tenant, msg, session, env, es_count_all
from wp import db, families as F, log, lease as L, projector

N_EVENTS = 6
ROUNDS_C = 5


def step_fd4ed53(self):
    """Projector.step as shipped at fd4ed53 (the control)."""
    if self.lease is None and not self.start():
        return 0
    try:
        L.renew(self.conn, self.lease, self.ttl)
        return self.derive() + self.mirror_events() + self.mirror_facts()
    except (projector.StaleWrite, F.StaleEpoch, L.LeaseLost):
        self.lease = None
        return -1


def _setup():
    conn, ks, es = env()
    t = tenant("psr")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"note {i}", "U") for i in range(N_EVENTS)])
    return conn, ks, es, t


def _lease_row(t):
    adm = db.admin()
    return adm.execute("select epoch, holder from leases where tenant_id=%s and scope=%s", (t, L.PROJECTOR)).fetchone()


def once(case):
    conn, ks, es, t = _setup()
    conn2, ks2, es2 = env()
    p1 = projector.Projector(conn, ks, es, t)
    takers, calls = [], {"p1": 0}
    real = F.fence_es

    def take_over():
        p2 = projector.Projector(conn2, ks2, es2, t)
        F.fence_es = real                       # the taker's own start() uses the real CAS
        try:
            ok = p2.start(force=True)
        finally:
            F.fence_es = wrapped
        assert ok, "forced takeover did not return a lease"
        takers.append(p2)

    def wrapped(es_, t_, epoch):
        calls["p1"] += 1
        if case in ("A", "C"):
            take_over()
            return real(es_, t_, epoch)
        real(es_, t_, epoch)
        take_over()                             # case B: after P1's CAS, before its cursor insert

    F.fence_es = wrapped
    try:
        try:
            out = ("returned", p1.run(max_rounds=ROUNDS_C)) if case == "C" else ("returned", p1.step())
        except Exception as ex:  # noqa: BLE001
            out = ("raised", type(ex).__name__)
    finally:
        F.fence_es = real
    row = _lease_row(t)
    taker = takers[-1] if takers else None
    rec = {"case": case, "outcome": f"{out[0]}:{out[1]}", "p1_starts": calls["p1"], "p1_holds_lease": p1.lease is not None,
           "lease_epoch": row[0] if row else None, "lease_holder_is_taker": bool(row and taker and row[1] == taker.lease.holder)}
    if case == "C":
        rec["holds"] = out == ("returned", False) and calls["p1"] == ROUNDS_C and not rec["p1_holds_lease"] and rec["lease_holder_is_taker"]
    else:
        taker.run(max_rounds=50)
        rec["events_in_es"] = es_count_all(es2, t, {"term": {"kind": "message"}})
        taker.stop()
        rec["holds"] = (out == ("returned", -1) and not rec["p1_holds_lease"] and rec["lease_holder_is_taker"]
                        and rec["events_in_es"] == N_EVENTS)
    for c in (conn, conn2):
        c.close()
    return rec


def arm(r, control):
    real_step = projector.Projector.step
    if control:
        projector.Projector.step = step_fd4ed53
    try:
        out = {}
        for case in ("A", "B", "C"):
            runs = []
            for _ in range(r):
                try:
                    runs.append(once(case))
                except Exception as ex:  # noqa: BLE001  (a harness error, counted, never a hold)
                    runs.append({"case": case, "holds": None, "error": f"{type(ex).__name__}: {str(ex)[:160]}"})
            out[case] = {"runs": runs, "holds": sum(x["holds"] is True for x in runs),
                         "violated": sum(x["holds"] is False for x in runs), "errors": sum(x["holds"] is None for x in runs)}
        return out
    finally:
        projector.Projector.step = real_step


def main(r):
    res = {"R": r, "fixed": arm(r, False), "control": arm(r, True),
           "control_patch": "Projector.step replaced by the fd4ed53 body (start() outside the stale-error handler)"}
    why = []
    for case in ("A", "B", "C"):
        f, c = res["fixed"][case], res["control"][case]
        if f["holds"] != r:
            why.append(f"fixed {case} holds {f['holds']}/{r} (violated {f['violated']}, errors {f['errors']})")
        if c["violated"] != r:
            why.append(f"control {case} violated {c['violated']}/{r} (errors {c['errors']})")
    res["verdict"] = "PASS" if not why else "FAIL"
    res["why"] = why
    print("projector_start_race", res["verdict"], why, {k: {c: (v[c]["holds"], v[c]["violated"], v[c]["errors"])
                                                          for c in ("A", "B", "C")} for k, v in (("fixed", res["fixed"]), ("control", res["control"]))},
          flush=True)
    write_result("projector_start_race", res)
    return res


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
