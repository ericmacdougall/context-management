"""Projector under Elasticsearch faults (THRESHOLDS amendment 21; review-2 N1, goodqa#16302).

At 1fcf3f7 families.fence_es turned every ESError of the alias request into StaleEpoch and Projector._bulk turned every
refused item (other than 409) into StaleEpoch. step() caught it, forgot its lease and returned -1, while the lease row
in Postgres stayed live until its TTL. The next acquires returned None, so run() saw two idle rounds and returned True
with events still pending. A bulk request that raised propagated, but left the lease live for its TTL.

Deterministic, one process, separate connections. The fault is injected on P1's OWN ES client object only, so the
re-read of the aliases, the taker and the fresh projector P3 all see the real Elasticsearch:
  CAS    the first alias request raises ESError 503 unavailable_shards_exception
  ITEM   the first events bulk returns every item as 429 es_rejected_execution_exception (nothing is sent)
  REQ    the first bulk request raises ESError 503
  CONN   the first bulk request raises ConnectionResetError
  STALE  a real forced takeover by P2 between P1's read and P1's bulk (positive control: holds in both arms)
Property (CAS, ITEM, REQ, CONN): P1.run(10) raises; P1 holds no lease and the lease row is no longer live; a fresh
projector P3 (not forced) holds the lease after its first step; P3.run(50) returns True; every event is in Elasticsearch
exactly once. STALE: P1.step() returns -1 without raising, P1 holds no lease, the lease row's holder is P2 and still
live after P1's handler, and P2 then projects every event exactly once.
Control (one mechanism): families.fence_es, Projector._bulk and Projector.step replaced by their 1fcf3f7 bodies,
verbatim (below).
usage: python -m exp.projector_es_fault [R]"""
import sys

from exp.common import write_result
from exp.util import tenant, msg, session, env, es_count_all
from wp import db, families as F, log, lease as L, projector
from wp.es import ESError

N_EVENTS = 6
CASES = ("CAS", "ITEM", "REQ", "CONN", "STALE")
FAULTS = ("CAS", "ITEM", "REQ", "CONN")


# ------------------------------------------------------------ the 1fcf3f7 bodies (the control), verbatim
def fence_es_1fcf3f7(es, t, epoch):
    actions = []
    for f in F.FAMILIES:
        F.ensure_family(es, t, f)
        st = F._family_state(es, t, f)
        if len(st) != 1:
            raise RuntimeError(f"family {f}: {len(st)} indices")
        (idx, als), = st.items()
        seen = F._walias_epochs(t, f, als)
        if any(e > epoch for _, e in seen):
            raise F.StaleEpoch(f"{f}: newer write alias than e{epoch}")
        if any(e == epoch for _, e in seen):
            continue
        for a, _ in seen:
            actions.append({"remove": {"index": idx, "alias": a, "must_exist": True}})
        actions.append({"add": {"index": idx, "alias": F.write_alias(t, f, epoch), "is_write_index": True}})
    if not actions:
        return
    try:
        es.aliases(actions)
    except ESError as e:
        raise F.StaleEpoch(f"alias CAS failed: {e.etype}") from None


def bulk_1fcf3f7(self, family, actions):
    if not actions:
        return
    ok, bad = self.es.bulk_fenced(actions)
    self.stats["docs_written"] += ok
    hard = [b for b in bad if b[1] != 409]
    if hard:
        self.stats["stale_bulk_items"] += len(hard)
        raise F.StaleEpoch(f"{family}: {len(hard)} items refused ({hard[0][2]})")
    self.es.req("POST", f"/{F.read_alias(self.t, family)}/_refresh")


def step_1fcf3f7(self):
    try:
        if self.lease is None and not self.start():
            return 0
        L.renew(self.conn, self.lease, self.ttl)
        return self.derive() + self.mirror_events() + self.mirror_facts()
    except (projector.StaleWrite, F.StaleEpoch, L.LeaseLost):
        self.lease = None
        return -1


# ------------------------------------------------------------ fault injection on one client object
def inject(es, case):
    """Replace one method on THIS ES instance; the first call fails, later calls pass through. Returns the counter."""
    n = {"injected": 0}
    if case == "CAS":
        real = es.aliases

        def aliases(actions):
            if not n["injected"]:
                n["injected"] += 1
                raise ESError(503, "unavailable_shards_exception", "injected")
            return real(actions)
        es.aliases = aliases
    elif case in ("ITEM", "REQ", "CONN"):
        real = es.bulk_fenced

        def bulk_fenced(actions, refresh=False):
            if not n["injected"]:
                n["injected"] += 1
                if case == "ITEM":
                    return 0, [(next(iter(a)), 429, "es_rejected_execution_exception") for a, _ in actions]
                if case == "REQ":
                    raise ESError(503, "unavailable_shards_exception", "injected")
                raise ConnectionResetError("injected")
            return real(actions, refresh)
        es.bulk_fenced = bulk_fenced
    return n


def _lease_row(t):
    adm = db.admin()
    try:
        return adm.execute("select epoch, holder, expires_at > clock_timestamp() from leases where tenant_id=%s and scope=%s",
                           (t, L.PROJECTOR)).fetchone()
    finally:
        adm.close()


def once(case):
    conn, ks, es = env()
    t = tenant("pef")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"note {i}", "U") for i in range(N_EVENTS)])
    conn2, ks2, es2 = env()
    conn3, ks3, es3 = env()
    rec = {"case": case}
    try:
        if case == "STALE":
            takers = []

            def hook(name, _data=None):
                if name == "mirror_before_bulk" and not takers:
                    p2 = projector.Projector(conn2, ks2, es2, t)
                    assert p2.start(force=True), "forced takeover did not return a lease"
                    takers.append(p2)
            p1 = projector.Projector(conn, ks, es, t, hook=hook)
            try:
                out = ("returned", p1.step())
            except Exception as ex:  # noqa: BLE001
                out = ("raised", type(ex).__name__)
            row = _lease_row(t)
            p2 = takers[0] if takers else None
            rec.update(outcome=f"{out[0]}:{out[1]}", p1_holds_lease=p1.lease is not None,
                       holder_is_taker=bool(row and p2 and row[1] == p2.lease.holder), lease_live_after=bool(row and row[2]))
            rec["taker_run"] = p2.run(max_rounds=50) if p2 else None
            rec["events_in_es"] = es_count_all(es3, t, {"term": {"kind": "message"}})
            if p2:
                p2.stop()
            rec["holds"] = (out == ("returned", -1) and not rec["p1_holds_lease"] and rec["holder_is_taker"]
                            and rec["lease_live_after"] and rec["taker_run"] is True and rec["events_in_es"] == N_EVENTS)
            return rec
        p1 = projector.Projector(conn, ks, es, t)
        n = inject(es, case)
        try:
            out = ("returned", p1.run(max_rounds=10))
        except Exception as ex:  # noqa: BLE001
            out = ("raised", type(ex).__name__)
        row = _lease_row(t)
        rec.update(outcome=f"{out[0]}:{out[1]}", injected=n["injected"], p1_holds_lease=p1.lease is not None,
                   lease_live_after=bool(row and row[2]),
                   events_after_p1=es_count_all(es3, t, {"term": {"kind": "message"}}))
        p3 = projector.Projector(conn3, ks3, es3, t)
        try:
            first = p3.step()
            rec["p3_first_step"], rec["p3_lease_after_first_step"] = first, p3.lease is not None
            rec["p3_run"] = p3.run(max_rounds=50)
        finally:
            p3.stop()
        rec["events_in_es"] = es_count_all(es3, t, {"term": {"kind": "message"}})
        rec["holds"] = (out[0] == "raised" and n["injected"] == 1 and not rec["p1_holds_lease"] and not rec["lease_live_after"]
                        and rec["p3_lease_after_first_step"] and rec["p3_run"] is True and rec["events_in_es"] == N_EVENTS)
        return rec
    finally:
        for c in (conn, conn2, conn3):
            c.close()


def arm(r, control):
    real = (F.fence_es, projector.Projector._bulk, projector.Projector.step)
    if control:
        F.fence_es, projector.Projector._bulk, projector.Projector.step = fence_es_1fcf3f7, bulk_1fcf3f7, step_1fcf3f7
    try:
        out = {}
        for case in CASES:
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
        F.fence_es, projector.Projector._bulk, projector.Projector.step = real


def main(r):
    res = {"R": r, "fixed": arm(r, False), "control": arm(r, True),
           "control_patch": "families.fence_es, Projector._bulk and Projector.step replaced by their 1fcf3f7 bodies"}
    why = []
    for case in CASES:
        f, c = res["fixed"][case], res["control"][case]
        if f["holds"] != r:
            why.append(f"fixed {case} holds {f['holds']}/{r} (violated {f['violated']}, errors {f['errors']})")
        if case in FAULTS and c["violated"] != r:
            why.append(f"control {case} violated {c['violated']}/{r} (errors {c['errors']})")
        if case == "STALE" and c["holds"] != r:
            why.append(f"control STALE holds {c['holds']}/{r} (positive control; errors {c['errors']})")
    res["verdict"] = "PASS" if not why else "FAIL"
    res["why"] = why
    print("projector_es_fault", res["verdict"], why, {k: {c: (v[c]["holds"], v[c]["violated"], v[c]["errors"]) for c in CASES}
                                                      for k, v in (("fixed", res["fixed"]), ("control", res["control"]))}, flush=True)
    write_result("projector_es_fault", res)
    return res


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
