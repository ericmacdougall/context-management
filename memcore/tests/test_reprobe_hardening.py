"""Deploy part 3, second round (review of f80e34d): findings F1 to F3 and the smaller items. Same scripted node and fake clock as
test_reprobe. Every test is written to FAIL BY ASSERTION on the reviewed code (see memcore/tests/mutation_check_reprobe.py)."""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from memcore.hosts import basic_service
from memcore.reprobe import ReprobingTier
from memcore.tests.test_reprobe import Clock, ScriptedEs, args


class Hardening(unittest.TestCase):
    def test_thousands_of_consecutive_failures_never_end_the_loop_or_overflow_the_delay(self):
        s = ScriptedEs({"up": False, "type": "basic", "status": "active"})
        clk = Clock()
        tier = ReprobingTier(s, interval_s=60.0, backoff_s=1.0, clock=clk)
        for i in range(5000):
            clk.t = max(clk.t, tier._due)
            try:
                tier.refresh()
            except BaseException as e:  # noqa: BLE001
                self.fail("refresh raised on consecutive failure %d: %r" % (i + 1, e))
        self.assertEqual(tier.failures, 5000)
        self.assertEqual(tier._due - clk.t, 60.0, "the delay saturates at the interval")
        s.node.update(up=True, type="trial")
        clk.t = tier._due
        self.assertEqual(tier.refresh()["tier"], "enterprise", "and recovers afterwards")
        self.assertEqual(tier.failures, 0)

    def test_an_exception_inside_a_refresh_is_recorded_and_the_next_refresh_runs(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        boom = [True]
        real = s.reset_probe

        def flaky():
            if boom[0]:
                boom[0] = False
                raise RuntimeError("adapter hook failed")
            real()
        s.reset_probe = flaky
        clk = Clock()
        tier = ReprobingTier(s, interval_s=60.0, backoff_s=1.0, clock=clk)
        try:
            first = tier.refresh()
        except BaseException as e:  # noqa: BLE001
            self.fail("a failing refresh escaped: %r" % (e,))
        self.assertIsNone(first)
        self.assertEqual(tier.last_error, "RuntimeError")
        clk.t += 1.5
        self.assertEqual(tier.maybe_refresh()["tier"], "enterprise")

    def test_the_tracker_refuses_unusable_intervals(self):
        s = ScriptedEs({"up": True, "type": "basic", "status": "active"})
        for bad in (0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, None, "60"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ReprobingTier(s, interval_s=bad)
            with self.assertRaises(ValueError, msg="backoff %r" % (bad,)):
                ReprobingTier(s, interval_s=10, backoff_s=bad)

    def test_the_service_refuses_negative_nan_and_inf_at_start_up_and_logs_zero_loudly(self):
        for bad in ("-1", "-0.001", "nan", "inf", "-inf"):
            with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", new=io.StringIO()):
                basic_service.main(["--out", tempfile.mkdtemp(), "--es", "http://x", "--es-auth-file", "f", "--reprobe-interval-s", bad])
            self.assertEqual(cm.exception.code, 2, bad)
        node = {"up": True, "type": "trial", "status": "active"}
        a = mock.Mock(es="http://x", es_auth_file="f", es_ca=None, tenant="t1", ingest=None, logical="events", dims=8)
        with mock.patch("memcore.adapters.elasticsearch_search.ElasticsearchSearch", lambda *x, **k: ScriptedEs(node)):
            store, rep = basic_service.build(a)
        err = io.StringIO()
        self.assertIsNone(basic_service.start_reprobe(args(interval=0), store, rep, stderr=err))
        self.assertIn("DISABLED", err.getvalue())
        self.assertIn("enterprise", err.getvalue())

    def test_a_failed_publication_is_retried_without_another_tier_change(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        clk = Clock()
        published, attempts = [], []

        def on_change(rep):
            attempts.append(rep["tier"])
            if len(attempts) <= 2:
                raise OSError("disk full")
            published.append(rep["tier"])
        tier = ReprobingTier(s, interval_s=60.0, backoff_s=1.0, clock=clk, on_change=on_change)
        tier.refresh()                                              # enterprise: publication attempt 1 fails
        self.assertEqual(published, [])
        for _ in range(3):                                          # the tier does not change again
            clk.t += 1.5
            tier.maybe_refresh()
        self.assertEqual(published, ["enterprise"], "retried at the short interval until it succeeded, with no tier change")
        self.assertEqual(attempts, ["enterprise"] * 3)
        clk.t += 61
        tier.maybe_refresh()
        self.assertEqual(attempts, ["enterprise"] * 3, "unchanged tier, accepted publication: not offered again")

    def test_after_a_lapse_a_failing_file_update_cannot_leave_the_enterprise_claim_in_place(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        clk = Clock()
        state = {"claim": "enterprise", "fail": True}

        def on_change(rep):
            if state["fail"]:
                raise OSError("read-only file system")
            state["claim"] = rep["tier"]
        tier = ReprobingTier(s, interval_s=60.0, backoff_s=1.0, clock=clk, on_change=on_change,
                             initial={"tier": "enterprise", "licence_type": "trial", "licence_status": "active"})
        node["status"] = "expired"
        clk.t += 61
        self.assertEqual(tier.maybe_refresh()["tier"], "basic")
        self.assertEqual(state["claim"], "enterprise", "witness: the publication failed")
        state["fail"] = False
        clk.t += 2
        tier.maybe_refresh()
        self.assertEqual(state["claim"], "basic")

    def test_reset_probe_clears_the_stale_diagnostics_too(self):
        s = ScriptedEs({"up": True, "type": "basic", "status": "active"})
        s._probe, s._probe_at, s.probe_error = False, 123.0, "HTTP 403: old"
        s.reset_probe()
        self.assertEqual((s._probe, s._probe_at, s.probe_error, s.fusion_location), (None, 0.0, None, "unknown"))

    def test_an_unchanged_enterprise_answer_leaves_the_adapter_latch_alone(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        resets = []
        real = s.reset_probe
        s.reset_probe = lambda: (resets.append(1), real())
        clk = Clock()
        tier = ReprobingTier(s, interval_s=10.0, clock=clk)
        tier.refresh()
        self.assertEqual(len(resets), 1, "the change to enterprise clears it once")
        for _ in range(4):
            clk.t += 11
            tier.maybe_refresh()
        self.assertEqual(len(resets), 1, "four unchanged licensed answers: no further reset")

    def test_a_refresh_that_finishes_after_stop_applies_nothing(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        strategy_before = s.strategy
        tier = ReprobingTier(s, interval_s=10.0, clock=Clock())
        orig = s._req

        def stop_during_the_request(method, path, body=None):
            tier._stop.set()                                        # stop() arrives while the request is in flight
            return orig(method, path, body)
        s._req = stop_during_the_request
        self.assertIsNone(tier.refresh())
        self.assertEqual(s.strategy, strategy_before)
        self.assertIsNone(tier.report)

    def test_the_default_clock_is_monotonic(self):
        self.assertIs(ReprobingTier(ScriptedEs({"up": True, "type": "basic", "status": "active"})).clock, time.monotonic)

    def test_the_capabilities_file_is_not_rewritten_while_the_tier_is_unchanged(self):
        node = {"up": True, "type": "trial", "status": "active"}
        a = mock.Mock(es="http://x", es_auth_file="f", es_ca=None, tenant="t1", ingest=None, logical="events", dims=8)
        with mock.patch("memcore.adapters.elasticsearch_search.ElasticsearchSearch", lambda *x, **k: ScriptedEs(node)):
            store, rep = basic_service.build(a)
        out = tempfile.mkdtemp()
        clk = Clock()
        with mock.patch("memcore.reprobe.ReprobingTier.start", lambda self, poll_s=0.2: None):
            tier = basic_service.start_reprobe(args(out=out), store, rep, stderr=io.StringIO(), clock=clk)
        for _ in range(3):
            clk.t += 61
            tier.maybe_refresh()
        self.assertEqual(os.listdir(out), [], "unchanged licensed tier: nothing to publish")


if __name__ == "__main__":
    unittest.main()
