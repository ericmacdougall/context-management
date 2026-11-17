"""verify/pgdiff.py and verify/mig.py (amendments 5 and 6): a well-formed passing artifact is PASS, and each single broken observation turns
it FAIL, so the independent verifiers are not vacuous. Stdlib only; no database. Review-3 N8: a control "caught" only by error outcomes,
or a privilege probe that failed with another exception, is not accepted; N4: the clean migration arm must show the refused rollback
after a post-migration write, and control D must show the unchecked rollback losing it.

GoodQA #16096 class B: the pg_diff fixture is the pinned design (tests/test_evidence_pgdiff.py: 5 seeds x 60 logs, every B1 scenario at
its depth, the identity fields exp/pg_diff.py appends), so each break below is judged on exact identities (checks.evidence ok) and
fails for its own reason; the breaks that remove a log or narrow the run fail on the evidence gate, which names them."""
import importlib.util
import os
import unittest

from tests.test_evidence_pgdiff import DOC as PG_DOC, ROWS as PG_ROWS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location("vt_" + name, os.path.join(HERE, "verify", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m



def _edit(pick, change, rows=None):
    """The rows with change(copy) in place of every row pick selects (the identity fields are kept unless change edits them)."""
    return [change(list(r)) if pick(r) else r for r in (PG_ROWS if rows is None else rows)]


def _put(**pos):
    def change(r):
        for k, v in pos.items():
            r[int(k[1:])] = v
        return r
    return change


B2_T0 = lambda r: r[0] == "t" and r[1] == "b2" and r[2] == "61/0" and r[7] == 0        # noqa: E731
B2_V = lambda r: r[0] == "v" and r[1] == "b2" and r[2] == "61/0"                        # noqa: E731
B2_L = lambda r: r[0] == "l" and r[1] == "b2" and r[2] == "61/0"                        # noqa: E731
CTL_B2_DIFF = lambda r: r[0] == "t" and r[1] == "ctl:b2" and r[5] == "decided_accept"   # noqa: E731  the arm's one semantic difference
CTL_R2_DIFF = lambda r: r[0] == "t" and r[1] == "ctl:b1:r2" and r[3] != r[5]           # noqa: E731

PROBES = ("select count(*) from dc_pre_r3_7.events", "select count(*) from dc.events", "select dc._apply_event('t1', '{}'::jsonb)")
CK1 = {"anchor_check": {"ok": True, "anchored": 5}, "view_equals_memory_fold": True, "app_denied": {q: "InsufficientPrivilege" for q in PROBES},
       "api_request": None, "api_event": None}
CK = dict(CK1, api_request="proposed", api_event="alice/mig-chk-1")
FW = {"tenants": {"t1": {"events_replayed": 9}}}
CLEAN = {"kind": "clean", "seed": 41, "checks1": CK1, "checks2": CK, "forward1": FW, "forward2": FW, "forward_twice_refused": "schema exists",
         "fp_before": "h", "fp_after_rollback": "h", "old_engine_after_rollback": True,
         "rollback_after_write": "written after the migration (events, vals): a rollback would discard those writes", "write_kept": True}
C1 = {"kind": "control", "seed": 41, "control": "forged_grant", "refused": "stored as granted, judged unauthorized by the definer core",
      "fp_before": "a", "fp_after": "a"}
C2 = {"kind": "control", "seed": 41, "control": "head_tampered", "refused": "state rebuilt from the log differs from the old tables in ['head']",
      "fp_before": "a", "fp_after": "a"}
C3 = {"kind": "control", "seed": 41, "control": "rollback_unchecked", "api_request": "proposed", "api_event": "alice/mig-ctl-1", "written": True,
      "rolled_back": True, "write_lost": True}
DM = {"result": {"verdict": "PASS", "outcome_codes": ["proposed", "retracted", "unauthorized"], "applied_codes": ["proposed", "retracted"]},
      "params": {"seeds": [41, 42, 43]}}
# The design has 3 seeds (THRESHOLDS amendment 6; verify/mig.py pins it since GoodQA #16096 class B): the cases below break seed 41's rows
# and add seeds 42 and 43 unbroken.
OTHER_SEEDS = [dict(r, seed=s) for s in (42, 43) for r in (CLEAN, C1, C2, C3)]


class PgDiffVerifier(unittest.TestCase):
    V = _load("pgdiff")

    def out(self, rows, doc=None):
        return self.V.verify(doc or PG_DOC, rows)

    def v(self, rows, doc=None):
        return self.out(rows, doc)["verdict"]

    def judged(self, rows, doc=None):
        """The verdict of a row set whose identities are exact: the evidence gate (absent before #16096) must not be what decides it."""
        out = self.out(rows, doc)
        ev = out.get("checks", {}).get("evidence")
        self.assertTrue(ev is None or ev.get("ok"), ev)
        return out["verdict"]

    def test_good_passes(self):
        self.assertEqual(self.judged(PG_ROWS), "PASS")

    def test_each_break_fails(self):
        for pick, change, what in ((B2_T0, _put(r5="z"), "code differs"), (B2_T0, _put(r5="error:X"), "error code"),
                                   (B2_V, _put(r4="y"), "view differs"), (B2_L, _put(r3=[], r4=[], r5=False), "chain fails"),
                                   (B2_L, _put(r3=[], r4=[["e", "a"]]), "applied events differ"),
                                   (lambda r: r[0] == "t" and r[1] == "b1:r1" and r[7] == 0 and r[8] == 0, _put(r5="b"), "B1 code differs"),
                                   (CTL_B2_DIFF, lambda r: r[:5] + [r[3]] + r[6:], "ctl:b2 shows no difference")):
            self.assertEqual(self.judged(_edit(pick, change)), "FAIL", what)
        self.assertEqual(self.v([r for r in PG_ROWS if not (r[1] == "b2" and r[2] == "61/1")]), "FAIL")   # a missing B2 log
        self.assertEqual(self.v(PG_ROWS, dict(PG_DOC, params=dict(PG_DOC["params"], scenarios_only="r1"))), "FAIL")   # a partial run

    def test_n8_control_caught_only_by_errors_fails(self):
        """0a20b64 counted error:* outcomes as control differences: a control arm whose broken run only errored looked 'caught'."""
        rows = _edit(CTL_B2_DIFF, _put(r5="error:OperationalError"))
        rows = _edit(CTL_R2_DIFF, _put(r5="error:OperationalError"), rows)
        self.assertEqual(self.judged(rows), "FAIL")

    def test_n8_each_control_arm_must_catch_semantically(self):
        rows = _edit(CTL_R2_DIFF, lambda r: r[:5] + [r[3]] + r[6:])                    # one arm shows nothing
        self.assertEqual(self.judged(rows), "FAIL")
        rows = _edit(lambda r: r[0] == "v" and r[1] == "ctl:b1:r2" and r[5] == 3, _put(r4="error:TypeError"), rows)   # ... or only an error
        self.assertEqual(self.judged(rows), "FAIL")
        rows = _edit(lambda r: r[0] == "v" and r[1] == "ctl:b2" and r[2] == "61/1", _put(r4="error:TypeError"))   # a real difference plus an error
        out = self.out(rows)
        self.assertTrue(out["checks"].get("evidence", {"ok": True})["ok"])
        self.assertEqual((out["verdict"], out["checks"]["control"]["ctl:b2"]["errors"], out["checks"]["control"]["ctl:b2"]["differences"]), ("PASS", 1, 1))


class MigVerifier(unittest.TestCase):
    V = _load("mig")

    def v(self, rows):
        return self.V.verify(DM, rows + OTHER_SEEDS)["verdict"]

    def test_good_passes(self):
        self.assertEqual(self.v([CLEAN, C1, C2, C3]), "PASS")

    def test_each_break_fails(self):
        for row in (dict(CLEAN, fp_after_rollback="z"), dict(CLEAN, checks2=dict(CK, api_request="unauthorized")),
                    dict(CLEAN, checks1=dict(CK1, app_denied=dict(CK1["app_denied"], **{PROBES[0]: False}))), dict(CLEAN, forward_twice_refused=False),
                    dict(CLEAN, checks1=dict(CK1, anchor_check={"ok": True, "anchored": 0})), dict(CLEAN, old_engine_after_rollback=False)):
            self.assertEqual(self.v([row, C1, C2, C3]), "FAIL")
        self.assertEqual(self.v([CLEAN, dict(C1, refused="tenant configs do not match"), C2, C3]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1, dict(C2, fp_after="b"), C3]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1, dict(C2, refused="error:X"), C3]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1, C3]), "FAIL")

    def test_n8_probe_must_fail_with_insufficient_privilege(self):
        """0a20b64 accepted any non-empty exception name as a denied privilege probe (an OperationalError, or a missing function)."""
        for name in ("OperationalError", "UndefinedFunction", "X"):
            ck = dict(CK1, app_denied=dict(CK1["app_denied"], **{PROBES[1]: name}))
            self.assertEqual(self.v([dict(CLEAN, checks1=ck), C1, C2, C3]), "FAIL", name)
        ck = dict(CK1, app_denied={q: "InsufficientPrivilege" for q in PROBES[:2]})          # a probe missing
        self.assertEqual(self.v([dict(CLEAN, checks1=ck), C1, C2, C3]), "FAIL")

    def test_n4_rollback_after_a_write_must_be_refused_and_the_write_kept(self):
        for row in (dict(CLEAN, rollback_after_write=False), dict(CLEAN, rollback_after_write="error:X written after the migration"),
                    dict(CLEAN, rollback_after_write="no dc_pre_r3_7 schema"), dict(CLEAN, write_kept=False),
                    dict(CLEAN, checks1=CK), dict(CLEAN, checks2=dict(CK, api_request="nothing_to_retract"))):
            self.assertEqual(self.v([row, C1, C2, C3]), "FAIL", row)
        for c3 in (dict(C3, write_lost=False), dict(C3, rolled_back="error:X"), dict(C3, written=False), dict(C3, api_request="unauthorized")):
            self.assertEqual(self.v([CLEAN, C1, C2, c3]), "FAIL", c3)
        self.assertEqual(self.v([CLEAN, C1, C2]), "FAIL")                             # control D missing


if __name__ == "__main__":
    unittest.main()
