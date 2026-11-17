"""Evidence identity properties of verify/mig.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS amendment 6,
exp/mig_rehearsal.py): 3 seeds; per seed one clean arm row (identity: seed) and one row per control forged_grant, head_tampered,
rollback_unchecked (identity: seed, control). The audit at ea4677a found that a later row overwrote a failing seed row."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS = [71, 72, 73]
PROBES = ("select count(*) from dc_pre_r3_7.events", "select count(*) from dc.events", "select dc._apply_event('t1', '{}'::jsonb)")
CK1 = {"anchor_check": {"ok": True, "anchored": 5}, "view_equals_memory_fold": True, "app_denied": {q: "InsufficientPrivilege" for q in PROBES},
       "api_request": None, "api_event": None}
CK2 = dict(CK1, api_request="proposed", api_event="alice/mig-chk-1")
FW = {"tenants": {"t1": {"events_replayed": 9}}}


def honest():
    rows = []
    for s in SEEDS:
        rows.append({"kind": "clean", "seed": s, "checks1": CK1, "checks2": CK2, "forward1": FW, "forward2": FW, "forward_twice_refused": "schema exists",
                     "fp_before": "h%d" % s, "fp_after_rollback": "h%d" % s, "old_engine_after_rollback": True,
                     "rollback_after_write": "written after the migration (events, vals): a rollback would discard those writes", "write_kept": True})
    for s in SEEDS:
        rows.append({"kind": "control", "seed": s, "control": "forged_grant", "refused": "stored as granted, judged unauthorized by the definer core",
                     "fp_before": "a%d" % s, "fp_after": "a%d" % s})
        rows.append({"kind": "control", "seed": s, "control": "head_tampered",
                     "refused": "state rebuilt from the log differs from the old tables in ['head']", "fp_before": "b%d" % s, "fp_after": "b%d" % s})
        rows.append({"kind": "control", "seed": s, "control": "rollback_unchecked", "api_request": "proposed", "api_event": "alice/mig-ctl-%d" % s,
                     "written": True, "rolled_back": True, "write_lost": True})
    return rows


DOC = {"name": "mig_rehearsal", "params": {"seeds": SEEDS, "fixture": "exp/fixtures/pgcore_5a80e52.py"},
       "result": {"verdict": "PASS", "applied_codes": ["proposed", "retracted"], "outcome_codes": ["proposed", "retracted", "unauthorized"]}}


class MigEvidence(Props, unittest.TestCase):
    V = load("mig")
    FAMILIES = (Fam("clean", lambda r: r.get("kind") == "clean", ["kind", "seed"], seed="seed"),
                Fam("control", lambda r: r.get("kind") == "control", ["kind", "seed", "control"], seed="seed", arm="control"))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def break_row(self, fam, row):
        return dict(row, fp_after_rollback="z") if fam.name == "clean" else dict(row, fp_after="changed", refused="error:X")

    def shrunk(self):
        two = dict(DOC, params=dict(DOC["params"], seeds=SEEDS[:2]))
        return [("two seeds declared and run", two, [r for r in honest() if r["seed"] != SEEDS[2]])]

    def test_audit_probe_failing_row_first_passing_later(self):
        """The audit's probe 2a: seed 71's clean row failing, a passing one after it (PASSED at ea4677a)."""
        rows = honest()
        failing = dict(rows[0], fp_after_rollback="z")
        self.bad([failing] + rows[1:] + [rows[0]], "failing clean row overwritten by a later passing one", ("duplicate identity",))


if __name__ == "__main__":
    unittest.main()
