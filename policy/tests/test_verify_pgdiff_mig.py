"""verify/pgdiff.py and verify/mig.py (amendment 5): a well-formed passing artifact is PASS, and each single broken observation turns it
FAIL, so the independent verifiers are not vacuous. Stdlib only; no database."""
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

CK = {"anchor_check": {"ok": True, "anchored": 5}, "view_equals_memory_fold": True, "app_denied": {"a": "InsufficientPrivilege", "b": "X", "c": "Y"},
      "api_request": "retracted"}
FW = {"tenants": {"t1": {"events_replayed": 9}}}
CLEAN = {"kind": "clean", "seed": 41, "checks1": CK, "checks2": CK, "forward1": FW, "forward2": FW, "forward_twice_refused": "schema exists",
         "fp_before": "h", "fp_after_rollback": "h", "old_engine_after_rollback": True}
C1 = {"kind": "control", "seed": 41, "control": "forged_grant", "refused": "stored as granted, judged unauthorized by the definer core",
      "fp_before": "a", "fp_after": "a"}
C2 = {"kind": "control", "seed": 41, "control": "head_tampered", "refused": "state rebuilt from the log differs from the old tables in ['head']",
      "fp_before": "a", "fp_after": "a"}
DM = {"result": {"verdict": "PASS", "outcome_codes": ["retracted", "unauthorized"]}, "params": {"seeds": [41]}}


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
        self.assertEqual(self.v([CLEAN, C1, C2]), "PASS")

    def test_each_break_fails(self):
        for row in (dict(CLEAN, fp_after_rollback="z"), dict(CLEAN, checks2=dict(CK, api_request="unauthorized")),
                    dict(CLEAN, checks1=dict(CK, app_denied={"a": False, "b": "X", "c": "Y"})), dict(CLEAN, forward_twice_refused=False),
                    dict(CLEAN, checks1=dict(CK, anchor_check={"ok": True, "anchored": 0})), dict(CLEAN, old_engine_after_rollback=False)):
            self.assertEqual(self.v([row, C1, C2]), "FAIL")
        self.assertEqual(self.v([CLEAN, dict(C1, refused="tenant configs do not match"), C2]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1, dict(C2, fp_after="b")]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1, dict(C2, refused="error:X")]), "FAIL")
        self.assertEqual(self.v([CLEAN, C1]), "FAIL")


if __name__ == "__main__":
    unittest.main()
