"""Evidence identity properties of verify/b3.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS B3 and its
amendments, exp/b3_pg.py): R = 50 rounds i = 0..49 of each of the nine scenarios and the three controls (identity (kind, scenario,
round)); 40 differential logs "<seed>/<li>", li = 0..40 // len(seeds) - 1 (identity (kind, log)); per log, its diff_event rows
i = 0..events-1, the count declared by the log's own row (category 2, parent-declared; "i" is new in exp/b3_pg.py: the stamped rows lack
it and the next stamped run must regenerate them); exactly one scen_rebuild row. Events per log are 5 here (the experiment draws 50 to
300)."""
import unittest

from tests.evidence_props import Fam, Props, load

R, SEEDS, LOGS, EVENTS = 50, [71], 40, 5
SCEN = ("S1_conflicting_decisions_contested", "S2_stale_token_refused", "S3_zombie_revoked_principal", "S4_same_idem_key_concurrent",
        "S4b_twins_on_two_requirements", "S5_resolve_race", "S6_tenant_isolation", "S7_api_supersede_race", "S8_api_twins")
CTL = ("ctl_no_head_lock", "ctl_no_advisory_lock", "ctl_idem_unprotected")
DOC = {"name": "b3_pg", "params": {"R": R, "diff_logs": LOGS, "seeds": SEEDS}, "result": {"verdict": "PASS"}}
T = ["2026-11-17T10:00:00.00000%d+00:00" % k for k in range(1, 5)]
W = {"a_locked": T[0], "b_blocked": T[1], "a_before_commit": T[2], "b_locked": T[3]}
W_API = {"b_blocked": T[0], "a_commit": T[1], "b_done": T[2], "b_done_before_commit": "False"}


def _info(scen, i):
    c = lambda a, b: {"a": list(a), "b": list(b)}       # noqa: E731
    sup = ("superseded", None)
    return {
        "S1_conflicting_decisions_contested": dict(codes=c(("decided_accept", None), ("contested_opened", None)), status="contested", version=2,
                                                   value_ref=None, candidates=["x", "y"], w=W),
        "S2_stale_token_refused": dict(codes=c(sup, ("stale", None)), version=3, value_ref="vY"),
        "S3_zombie_revoked_principal": dict(codes=c(("revoked", None), ("principal_revoked", None)), version=0, w=W),
        "S4_same_idem_key_concurrent": dict(codes=c(sup, ("duplicate" if i % 2 == 0 else "idem_conflict", "superseded")), event_rows=1, w=W),
        "S4b_twins_on_two_requirements": dict(codes=c(sup, ("idem_conflict", "superseded")), event_rows=1, w=W),
        "S5_resolve_race": dict(codes=c(("resolved", None), ("stale", None)), version=3, w=W),
        "S6_tenant_isolation": dict(codes=[["unauthorized"], ["tenant_mismatch"], ["unauthorized"]]),
        "S7_api_supersede_race": dict(codes=c(sup, ("contested_opened", None)), status="contested", version=2, candidates=["u1/Sa%d" % i, "u2/Sb%d" % i],
                                      w=W_API),
        "S8_api_twins": dict(codes=c(sup, ("duplicate", "superseded")), event_rows=1, w=W_API),
        "ctl_no_head_lock": dict(codes=c(("decided_accept", None), ("decided_accept", None)), status="current", w=W),
        "ctl_no_advisory_lock": dict(backstop_retries=1),
        "ctl_idem_unprotected": dict(codes=c(sup, sup), event_rows=2),
    }[scen]


def honest(rounds=R, logs=LOGS):
    rows = [{"kind": "round", "scenario": s, "round": i, "ok": True, "overlap": True, "info": _info(s, i)} for s in SCEN + CTL for i in range(rounds)]
    for s in SEEDS:
        for li in range(logs // len(SEEDS)):
            log = "%s/%d" % (s, li)
            rows += [{"kind": "diff_event", "log": log, "ev": {"id": "e%d" % k}, "mem": ["stale", None] if k == 0 else ["proposed", None],
                      "pg": ["stale", None] if k == 0 else ["proposed", None], "i": k} for k in range(EVENTS)]
            rows.append({"kind": "diff_log", "log": log, "events": EVENTS, "mismatch_events": 0, "pg_view": {"h": 1}, "mem_view": {"h": 1},
                         "rebuild_view": {"h": 1}, "chain": [EVENTS - 1, True], "applied_rows": EVENTS - 1, "audit_rows": 1, "refused": 1})
    rows.append({"kind": "scen_rebuild", "live": {"h": 2}, "rebuild": {"h": 2}, "chain": [9, True], "applied_rows": 9, "audit_rows": 3})
    return rows


class B3Evidence(Props, unittest.TestCase):
    V = load("b3")
    FAMILIES = (Fam("round", lambda r: r["kind"] == "round", ["kind", "scenario", "round"], index="round", arm="scenario", end=lambda s, r: R),
                Fam("diff_log", lambda r: r["kind"] == "diff_log", ["kind", "log"]),
                Fam("diff_event", lambda r: r["kind"] == "diff_event", ["kind", "log", "i"], index="i", end=lambda s, r: EVENTS),
                Fam("scen_rebuild", lambda r: r["kind"] == "scen_rebuild", ["kind"]))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def break_row(self, fam, row):
        if fam.name == "round":
            return dict(row, info=dict(row["info"], errs=["x"]))
        if fam.name == "diff_event":
            return dict(row, pg=["unauthorized", None])
        if fam.name == "diff_log":
            return dict(row, pg_view={"h": 9})
        return None

    def shrunk(self):
        return [("R = 25 declared and run", dict(DOC, params=dict(DOC["params"], R=25)), honest(rounds=25)),
                ("diff_logs = 10 declared and run", dict(DOC, params=dict(DOC["params"], diff_logs=10)), honest(logs=10))]

    def test_log_and_its_events_dropped_together(self):
        """A whole differential log (its row and its events) dropped: PASSED at ea4677a (it needed only one log)."""
        self.bad([r for r in honest() if r.get("log") != "71/7"], "log 71/7 dropped with its events", ("missing identity",))

    def test_events_beyond_the_declared_count(self):
        """An event row past the count its log declares is an extra identity."""
        rows = honest()
        k = next(j for j, r in enumerate(rows) if r["kind"] == "diff_event" and r["log"] == "71/3")
        self.bad(rows + [dict(rows[k], i=EVENTS)], "event 5 of a 5-event log", ("extra identity",))


if __name__ == "__main__":
    unittest.main()
