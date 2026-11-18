"""Evidence identity properties of verify/b2.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS B2 and its
amendments, exp/b2_rebuild.py): 600 logs "<seed>/<li>" per seed over 5 seeds; one enforced log row per log (identity (arm, log)); its
event rows i = 0..n-1, n declared by the log row (identity (arm, log, i); category 2, parent-declared); one svc_audit row for every fifth
log of the run order (logs[::5]); one ctl_log row per (control, log) for the eight pinned controls. No experiment change: the rows already
carry these fields. Events per log are 3 here (the experiment draws 5 to 300). At ea4677a the per-log code check also depended on ROW
ORDER (events were concatenated as read), so a shuffled honest set FAILED there: the order-invariance test is red too."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS, PER = [71, 72, 73, 74, 75], 600
APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch",
           "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal",
           "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve", "not_contested",
           "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
ALL = APPLIED + REFUSED
CONTROLS = ["agent_may_decide", "n2_no_cap", "n2_no_role", "m2_no_original", "stale_unchecked", "agent_may_supersede", "snapshot_drops_revocations",
            "restore_drops_idempotency"]
MODES = [["random", 0.7], ["cap", 0.15], ["contest", 0.15]]
DOC = {"name": "b2_rebuild", "params": {"seeds": SEEDS, "per_seed": PER, "modes": MODES, "controls": CONTROLS},
       "result": {"verdict": "PASS", "controls": {c: {} for c in CONTROLS}}}


def honest(seeds=SEEDS, per=PER):
    ids = ["%s/%d" % (s, li) for s in seeds for li in range(per)]
    rows = []
    for j, lid in enumerate(ids):
        codes = ["contested_opened", ALL[(2 * j) % len(ALL)], ALL[(2 * j + 1) % len(ALL)]]
        rows += [{"kind": "event", "arm": "enforced", "log": lid, "i": i, "ev": {"id": "e%d" % i}, "ref_code": c, "ref_orig": None, "prod_code": c,
                  "prod_orig": None, "ref_view": "h%d" % i, "prod_view": "h%d" % i} for i, c in enumerate(codes)]
        rows.append({"kind": "log", "arm": "enforced", "log": lid, "n": 3, "restores": 1, "first_mismatch": None, "live_sha": "a", "rebuild_sha": "a",
                     "prod_codes": codes, "ref_audit_codes": [c for c in codes if c in REFUSED]})
    rows += [{"kind": "svc_audit", "log": lid, "returned": ["proposed", "stale"], "refusals_returned": ["stale"], "audited_refusals": ["stale"]}
             for lid in ids[::5]]
    rows += [{"kind": "ctl_log", "arm": c, "log": lid, "live_sha": "a", "rebuild_sha": "a",
              "first_mismatch": {"i": 0, "ref": ["stale", None], "prod": ["decided_accept", None], "ref_view": {}, "prod_view": {}}}
             for c in CONTROLS for lid in ids]
    return rows


class B2Evidence(Props, unittest.TestCase):
    V = load("b2")
    FAMILIES = (Fam("log", lambda r: r["kind"] == "log", ["kind", "arm", "log"], arm="arm"),
                Fam("event", lambda r: r["kind"] == "event", ["kind", "arm", "log", "i"], index="i", arm="arm", end=lambda s, r: 3),
                Fam("svc_audit", lambda r: r["kind"] == "svc_audit", ["kind", "log"]),
                Fam("ctl_log", lambda r: r["kind"] == "ctl_log", ["kind", "arm", "log"], arm="arm"))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def break_row(self, fam, row):
        if fam.name == "event":
            return dict(row, prod_view="other")
        if fam.name == "log":
            return dict(row, rebuild_sha="b")
        if fam.name == "svc_audit":
            return dict(row, audited_refusals=[])
        return None

    def shrunk(self):
        ten = list(range(71, 81))
        return [("300 logs per seed over ten seeds declared and run", dict(DOC, params=dict(DOC["params"], seeds=ten, per_seed=300)),
                 honest(seeds=ten, per=300))]

    def test_log_moved_to_an_undeclared_id(self):
        """Log 71/11 (its events, log row and control rows) relabelled 75/600, a log the design never runs: still 3000 logs, so it
        PASSED at ea4677a."""
        rows = [dict(r, log="75/600") if r.get("log") == "71/11" else r for r in honest()]
        self.bad(rows, "log 71/11 relabelled 75/600", ("missing identity", "extra identity"))


if __name__ == "__main__":
    unittest.main()
