"""Offline unit tests (no node): oracle sensitivity and the re-probing tier tracker against a scripted /_license sequence.
Run (on t2-dev, from a directory holding licence/ and memcore/): PYTHONPATH=licence:. python3 licence/tests/test_offline.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import oracle  # noqa: E402
from memcore import capabilities  # noqa: E402
from reprobe import ReprobingTier  # noqa: E402


class FakeSearch:
    strategy = "auto"
    fusion_location = "unknown"

    def __init__(self, answers):
        self.answers, self.i = answers, 0

    def _req(self, method, path, body=None):
        a = self.answers[min(self.i, len(self.answers) - 1)]
        self.i += 1
        if isinstance(a, Exception):
            raise a
        return a


def L(t, s="active"):
    return {"license": {"type": t, "status": s}}


class OracleTests(unittest.TestCase):
    legs = (["a", "b", "c", "d"], ["c", "a", "e", "f"])

    def test_valid_rrf_order_accepted(self):
        sc = oracle.rrf_scores(list(self.legs))
        best = sorted(sc, key=lambda d: -sc[d])
        self.assertTrue(oracle.valid_topk(best[:3], list(self.legs), 3))

    def test_lexical_only_rejected(self):
        self.assertFalse(oracle.valid_topk(["a", "b", "c"], list(self.legs), 3))

    def test_wrong_order_rejected(self):
        self.assertFalse(oracle.valid_topk(["e", "a", "c"], list(self.legs), 3))

    def test_duplicate_or_short_rejected(self):
        self.assertFalse(oracle.valid_topk(["a", "a", "c"], list(self.legs), 3))
        self.assertFalse(oracle.valid_topk(["a", "c"], list(self.legs), 3))


class ReprobeTests(unittest.TestCase):
    def test_reviewed_apply_pins_client_forever_but_reprobe_recovers(self):
        err = ConnectionResetError("starting")
        reviewed, tracked = FakeSearch([err, L("trial")]), FakeSearch([err, L("trial")])
        rep = capabilities.probe(capabilities.es_get_json(reviewed))
        capabilities.apply(rep, reviewed)
        self.assertEqual(reviewed.strategy, "client")
        t = [0.0]
        tr = ReprobingTier(tracked, interval_s=5.0, backoff_s=1.0, clock=lambda: t[0])
        tr.refresh()
        self.assertEqual(tracked.strategy, "client")
        self.assertEqual(tr.report["probe_error"], "ConnectionResetError")
        t[0] = 0.5
        self.assertIsNone(tr.maybe_refresh())          # backoff not due yet
        t[0] = 1.1
        tr.maybe_refresh()
        self.assertEqual(tracked.strategy, "auto")     # node answered: trial
        self.assertEqual(reviewed.strategy, "client")  # reviewed code never re-probes: the control fails

    def test_lapse_noticed_without_a_403(self):
        s = FakeSearch([L("trial"), L("basic")])
        t = [0.0]
        tr = ReprobingTier(s, interval_s=5.0, clock=lambda: t[0])
        tr.refresh()
        self.assertEqual(s.strategy, "auto")
        t[0] = 5.1
        tr.maybe_refresh()
        self.assertEqual(s.strategy, "client")

    def test_never_enterprise_on_bad_answers(self):
        for bad in ({}, {"license": None}, {"license": {"type": "trial", "status": "expired"}}, {"license": {"type": ["trial"], "status": "active"}}):
            s = FakeSearch([bad])
            ReprobingTier(s).refresh()
            self.assertEqual(s.strategy, "client", bad)


if __name__ == "__main__":
    unittest.main()
