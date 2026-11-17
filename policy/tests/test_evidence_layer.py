"""verify/evidence.py itself (GoodQA #16096 class B): identities are typed, the exact-set gate names each reason, a malformed row is
unidentified or unclassified (never a crash), and verify/__main__.py's import gate covers the shared layer. Stdlib only."""
import ast
import importlib.util
import os
import unittest

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(POLICY, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _load("verify/evidence.py", "evidence_under_test")
MAIN = _load("verify/__main__.py", "verify_main_under_test")


class Layer(unittest.TestCase):
    def test_identities_are_typed(self):
        self.assertEqual(len({EV.key(1), EV.key(True), EV.key(1.0), EV.key("1")}), 4)
        self.assertEqual(EV.key("s", 0), EV.key("s", 0))
        for bad in (True, 1.0, "1", -1, None):
            self.assertIsNone(EV.index(bad), bad)
        self.assertEqual(EV.index(0), 0)

    def _gate(self, rows, expected):
        g = EV.Gate("x", {"params": {"seeds": [1]}})
        g.family("f", rows, lambda r: (r["seed"], r["i"]), expected)
        return g

    def test_each_reason_is_named(self):
        exp = {EV.key(1, i) for i in range(3)}
        good = [{"seed": 1, "i": i} for i in range(3)]
        self.assertTrue(self._gate(good, exp).ok)
        for rows, word in ((good + [dict(good[0])], "duplicate identity"), (good[:2], "missing identity"),
                           (good + [{"seed": 1, "i": 3}], "extra identity"), (good + [{"seed": 1}], "unidentified"),
                           (good[:2] + [{"seed": 1, "i": True}], "missing identity"), (good[:2] + [{"seed": 1, "i": 2.0}], "extra identity")):
            g = self._gate(rows, exp)
            self.assertFalse(g.ok, word)
            self.assertIn(word, " ".join(g.problems), word)

    def test_unclassified_rows_and_bad_seeds(self):
        g = EV.Gate("x", {"params": {"seeds": [1]}})
        g.split([{"k": 1}, ["list row"], None], lambda r: "f" if r["k"] == 1 else None)
        self.assertIn("unclassified", " ".join(g.problems))
        for sd, n in (([1, 1], 1), ([True], 1), ([1.0], 1), ([1], 2), (None, 1), ([], 1), ([1, 2], 3)):
            g = EV.Gate("x", {"params": {"seeds": sd}})
            self.assertEqual(g.seeds(n), [], sd)
            self.assertFalse(g.ok, sd)
        g = EV.Gate("x", {"params": {"seeds": ["dev-x-1", 71]}})
        self.assertEqual(g.seeds(2), ["dev-x-1", 71])
        self.assertTrue(g.ok)

    def test_pins_compare_exactly(self):
        g = EV.Gate("x", {"params": {"R": 50, "combos": [["a", "b"]]}})
        g.pinned({"R": 50, "combos": [("a", "b")]})
        self.assertTrue(g.ok)
        for got in (25, 50.0, True, None):
            g = EV.Gate("x", {"params": {"R": got}})
            g.pinned({"R": 50})
            self.assertFalse(g.ok, got)

    def test_wrong_experiment_name(self):
        self.assertFalse(EV.Gate("e1_lattice", {"name": "e2_egress"}).ok)

    def test_main_import_gate_covers_the_layer(self):
        self.assertTrue(MAIN._imports_ok(os.path.join(POLICY, "verify", "evidence.py")))
        self.assertFalse(MAIN._imports_ok(os.path.join(POLICY, "exp", "e1_lattice.py")))     # the gate refuses a module importing pol
        with open(os.path.join(POLICY, "verify", "__main__.py"), encoding="utf-8") as f:
            main = ast.unparse(ast.parse(f.read()))
        self.assertIn("evidence_ok = _imports_ok(os.path.join(HERE, 'evidence.py'))", main)
        self.assertIn("if not _imports_ok(vpath) or not evidence_ok:", main)


if __name__ == "__main__":
    unittest.main()
