"""Offline unit test: exp/races.py verdict order (amendment H4). A fenced violation is a FAIL whatever the overlap
counts; before H4 the overlap counts were tested first, so a fenced violation in a run with fewer than 20 overlapped
runs was reported INCONCLUSIVE. Only the pure verdict function is loaded (its source is cut out of exp/races.py, so the
stores and the experiment helpers are not imported). usage (from writepath/): python3 -m unittest tests.test_races_verdict"""
import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).resolve().parents[1] / "exp" / "races.py"


def load_verdict():
    tree = ast.parse(SRC.read_text())
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "verdict"]
    if not fn:
        raise AssertionError("exp/races.py has no verdict() function")
    ns = {}
    exec(compile(ast.Module(body=fn, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["verdict"]


def arm(overlapped=25, violated_all=0, errors=0, violated_overlapped=0):
    return {"overlapped": overlapped, "violated_all": violated_all, "errors": errors, "violated_overlapped": violated_overlapped}


class RacesVerdict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = staticmethod(load_verdict())

    def test_fenced_violation_with_few_overlaps_is_fail(self):
        self.assertEqual(self.v(arm(overlapped=3, violated_all=1), arm(overlapped=30, violated_overlapped=5)), "FAIL")

    def test_fenced_violation_when_control_has_few_overlaps_is_fail(self):
        self.assertEqual(self.v(arm(overlapped=30, violated_all=2), arm(overlapped=1)), "FAIL")

    def test_few_overlaps_without_violation_is_inconclusive(self):
        self.assertEqual(self.v(arm(overlapped=19), arm(overlapped=30, violated_overlapped=3)), "INCONCLUSIVE")

    def test_pass_needs_control_violation(self):
        self.assertEqual(self.v(arm(), arm(violated_overlapped=1)), "PASS")
        self.assertEqual(self.v(arm(), arm(violated_overlapped=0)), "FAIL")

    def test_fenced_error_with_enough_overlaps_is_fail(self):
        self.assertEqual(self.v(arm(errors=1), arm(violated_overlapped=4)), "FAIL")


if __name__ == "__main__":
    unittest.main()
