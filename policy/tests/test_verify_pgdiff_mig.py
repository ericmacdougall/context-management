"""verify/pgdiff.py and verify/mig.py (amendments 5 and 6): a well-formed passing artifact is PASS, and each single broken observation turns
it FAIL, so the independent verifiers are not vacuous. Stdlib only; no database. Review-3 N8: a control "caught" only by error outcomes,
or a privilege probe that failed with another exception, is not accepted; N4: the clean migration arm must show the refused rollback
after a post-migration write, and control D must show the unchecked rollback losing it."""
import copy
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location("vt_" + name, os.path.join(HERE, "verify", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PAR = {"per_seed": 1, "seeds": [1, 2], "b1_depth": {"r1": 2}, "scenarios_only": "all"}
ROWS = [["t", "b2", "1/0", "a", None, "a", None], ["v", "b2", "1/0", "x", "x"], ["l", "b2", "1/0", [["e", "a"]], [["e", "a"]], True],
        ["t", "b2", "2/0", "a", None, "a", None], ["v", "b2", "2/0", "x", "x"], ["l", "b2", "2/0", [], [], True],
        ["t", "b1:r1", 0, "a", None, "a", None], ["v", "b1:r1", 0, "x", "x"],
        ["t", "ctl:b2", "1/0", "a", None, "b", None], ["t", "ctl:b1:r2", 0, "a", None, "a", None]]
RES = {"verdict": "PASS", "b2": {"events": 2, "logs": 2}, "b1": [{"scenario": "r1", "transitions": 1, "states": 1}]}

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
      "params": {"seeds": [41]}}


class PgDiffVerifier(unittest.TestCase):
    V = _load("pgdiff")

    def v(self, rows, par=PAR):
        return self.V.verify({"result": RES, "params": par}, rows)["verdict"]

    def test_good_passes(self):
        self.assertEqual(self.v(ROWS), "PASS")

    def test_each_break_fails(self):
        for i, row in ((0, ["t", "b2", "1/0", "a", None, "z", None]), (0, ["t", "b2", "1/0", "a", None, "error:X", None]),
                       (1, ["v", "b2", "1/0", "x", "y"]), (2, ["l", "b2", "1/0", [], [], False]), (2, ["l", "b2", "1/0", [], [["e", "a"]], True]),
                       (6, ["t", "b1:r1", 0, "a", None, "b", None]), (8, ["t", "ctl:b2", "1/0", "a", None, "a", None])):
            rows = copy.deepcopy(ROWS)
            rows[i] = row
            self.assertEqual(self.v(rows), "FAIL", row)
        self.assertEqual(self.v(ROWS[:3] + ROWS[6:]), "FAIL")                       # a missing B2 log
        self.assertEqual(self.v(ROWS, dict(PAR, scenarios_only="r1")), "FAIL")      # a partial run


class MigVerifier(unittest.TestCase):
    V = _load("mig")

    def v(self, rows):
        return self.V.verify(DM, rows)["verdict"]

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
