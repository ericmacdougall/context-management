"""Deploy part 3 (goodqa#16096): the Basic start-up probe must be re-read. No network: a scripted Elasticsearch (a subclass of the
real ElasticsearchSearch whose _req answers from a node dict) and a fake clock. Assertions are on the PATH fused() takes
(result.fusion and the request shapes seen), never on a flag alone, because the adapter has its own licence latch.

PENDING_REAL_TEST D3-reprobe: after a restart against a node that is unreachable then licensed, and after a lapse then a return,
fused() follows the tier within the stated interval, with no 403 needed."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from memcore.adapters.elasticsearch_search import ElasticsearchSearch
from memcore.hosts import basic_service
from memcore.ports import BackendError
from memcore.reprobe import ReprobingTier

LICENCE_403 = "HTTP 403: " + json.dumps({"error": {"reason": "current license is non-compliant for [Reciprocal Rank Fusion (RRF)]"}})


class ScriptedEs(ElasticsearchSearch):
    """node = {"up": bool, "type": "basic"|"trial"|..., "status": "active"|"expired"}; .seen lists request kinds in order."""

    def __init__(self, node, **kw):
        super().__init__("http://scripted.invalid", lambda: "ApiKey x", tenant="t1", **kw)
        self.node, self.seen = node, []

    def _req(self, method, path, body=None):
        if not self.node["up"]:
            raise BackendError("connection failed: refused")
        if path == "/_license":
            self.seen.append("license")
            return {"license": {"type": self.node["type"], "status": self.node["status"]}}
        kind = "retriever" if "retriever" in (body or {}) else ("knn" if "knn" in (body or {}) else "lexical")
        self.seen.append(kind)
        if kind == "retriever" and not (self.node["type"] in ("trial", "enterprise") and self.node["status"] == "active"):
            raise BackendError(LICENCE_403)
        return {"hits": {"hits": [{"_id": "d1", "_score": 1.0, "_source": {"text": "x", "ts": "t"}}]}}

    def fused_path(self):
        """Run one fused() call and return (result.fusion, the request kinds it issued)."""
        before = len(self.seen)
        res = self.fused("events", "q", [0.1, 0.2], k=3)
        return res.fusion, self.seen[before:]


def args(**kw):
    ns = mock.Mock()
    ns.reprobe_interval_s = kw.get("interval", 60.0)
    ns.out = kw.get("out", tempfile.mkdtemp())
    return ns


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ReprobeFollowsTheTier(unittest.TestCase):
    def service_started_while_node_down(self, node, interval=60.0):
        """What basic_service does at start: build(), then start_reprobe(). The tracker is created but its thread is not
        started here: the test drives maybe_refresh() with the fake clock."""
        a = mock.Mock(es="http://x", es_auth_file="f", es_ca=None, tenant="t1", ingest=None, logical="events", dims=8)
        with mock.patch("memcore.adapters.elasticsearch_search.ElasticsearchSearch", lambda *x, **k: ScriptedEs(node)):
            store, rep = basic_service.build(a)
        clk = Clock()
        with mock.patch("memcore.reprobe.ReprobingTier.start", lambda self, poll_s=0.2: None):
            tier = basic_service.start_reprobe(args(interval=interval), store, rep, stderr=open(os.devnull, "w"), clock=clk)
        return store.search, rep, tier, clk

    def test_restart_while_node_unreachable_then_licensed_leaves_the_basic_path_within_the_backoff(self):
        node = {"up": False, "type": "trial", "status": "active"}
        search, rep, tier, clk = self.service_started_while_node_down(node)
        self.assertEqual(rep["tier"], "basic")
        self.assertEqual(rep["probe_error"], "BackendError")                      # witness: start-up saw an error, not Basic
        node["up"] = True                                                       # the node finishes starting; it is licensed
        self.assertEqual(search.fused_path()[0], "client")                       # still pinned until the tracker runs
        clk.t += 0.5
        self.assertIsNone(tier.maybe_refresh(), "not due yet")
        clk.t += 1.0                                                            # 1.5 s after start: past the 1 s retry
        self.assertEqual(tier.maybe_refresh()["tier"], "enterprise")
        fusion, kinds = search.fused_path()
        self.assertEqual(fusion, "backend")
        self.assertEqual(kinds, ["retriever", "retriever"])                      # the adapter probe query, then the fused query
        self.assertNotIn("lexical", kinds)

    def test_control_without_re_probing_the_service_stays_pinned_on_the_client_path(self):
        """The prior behaviour (reproduced by --reprobe-interval-s 0): the start-up answer is pinned for the life of the process."""
        node = {"up": False, "type": "trial", "status": "active"}
        a = mock.Mock(es="http://x", es_auth_file="f", es_ca=None, tenant="t1", ingest=None, logical="events", dims=8)
        with mock.patch("memcore.adapters.elasticsearch_search.ElasticsearchSearch", lambda *x, **k: ScriptedEs(node)):
            store, rep = basic_service.build(a)
        self.assertIsNone(basic_service.start_reprobe(args(interval=0), store, rep))
        node["up"] = True
        for _ in range(3):
            self.assertEqual(store.search.fused_path()[0], "client")             # a licensed node, yet always the Basic path
            self.assertNotIn("retriever", store.search.seen)

    def test_lapse_is_noticed_by_the_re_probe_not_by_a_403(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        clk = Clock()
        tier = ReprobingTier(s, interval_s=60.0, clock=clk)
        self.assertEqual(tier.maybe_refresh()["tier"], "enterprise")
        self.assertEqual(s.fused_path()[0], "backend")
        node["status"] = "expired"                                              # the licence lapses
        clk.t += 30
        self.assertIsNone(tier.maybe_refresh(), "inside the interval: not yet noticed")
        clk.t += 31
        self.assertEqual(tier.maybe_refresh()["tier"], "basic")
        fusion, kinds = s.fused_path()
        self.assertEqual(fusion, "client")
        self.assertEqual(sorted(kinds), ["knn", "lexical"])                      # no retriever request, so no 403 was needed

    def test_licence_returns_after_a_403_latched_the_adapter_and_the_next_call_uses_the_backend(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        clk = Clock()
        tier = ReprobingTier(s, interval_s=60.0, clock=clk)
        tier.maybe_refresh()
        node["status"] = "expired"
        self.assertEqual(s.fused_path()[0], "client")                            # in-call fallback: the adapter latch is now False
        self.assertIs(s._probe, False)
        clk.t += 61
        self.assertEqual(tier.maybe_refresh()["tier"], "basic")
        node["status"] = "active"                                               # licence back, 61 s after the latch (TTL is 300 s)
        clk.t += 61
        self.assertEqual(tier.maybe_refresh()["tier"], "enterprise")
        self.assertEqual(s.fused_path()[0], "backend")

    def test_control_without_clearing_the_adapter_latch_the_client_path_outlives_the_licence_lapse(self):
        node = {"up": True, "type": "trial", "status": "active"}
        s = ScriptedEs(node)
        s.reset_probe = lambda: None                                            # broken variant: strategy flips, latch kept
        clk = Clock()
        tier = ReprobingTier(s, interval_s=60.0, clock=clk)
        tier.maybe_refresh()
        node["status"] = "expired"
        s.fused_path()
        clk.t += 61
        tier.maybe_refresh()
        node["status"] = "active"
        clk.t += 61
        tier.maybe_refresh()
        self.assertEqual(s.strategy, "auto")
        self.assertEqual(s.fused_path()[0], "client", "the stale latch still holds the Basic path: reset_probe is what fixes this")


class RetrySchedule(unittest.TestCase):
    def test_probe_errors_retry_with_doubling_backoff_capped_at_the_interval(self):
        node = {"up": False, "type": "basic", "status": "active"}
        s = ScriptedEs(node)
        clk = Clock()
        tier = ReprobingTier(s, interval_s=10.0, backoff_s=1.0, clock=clk)
        gaps = []
        for _ in range(6):
            clk.t = max(clk.t, tier._due)
            before = clk.t
            tier.refresh()
            gaps.append(tier._due - before)
        self.assertEqual(gaps, [1.0, 2.0, 4.0, 8.0, 10.0, 10.0])
        node["up"] = True
        clk.t = tier._due
        tier.refresh()
        self.assertEqual(tier._due - clk.t, 10.0, "a definite answer returns to the stated interval")
        self.assertEqual(tier.failures, 0)

    def test_a_failing_refresh_does_not_stop_the_loop(self):
        node = {"up": True, "type": "basic", "status": "active"}
        s = ScriptedEs(node)
        calls = []

        def on_change(rep):
            calls.append(rep["tier"])
            if len(calls) == 1:
                raise RuntimeError("operator hook exploded")
        tier = ReprobingTier(s, interval_s=0.05, backoff_s=0.05, on_change=on_change, initial=None)
        tier.report = None
        tier.start(poll_s=0.01)
        try:
            node["type"], node["status"] = "trial", "active"
            deadline = time.time() + 5
            while time.time() < deadline and len(tier.history) < 6:
                time.sleep(0.02)
            node["type"] = "basic"
            deadline = time.time() + 5
            while time.time() < deadline and tier.tier() != "basic":
                time.sleep(0.02)
        finally:
            tier.stop()
        self.assertTrue(any(h[5] == "publish:RuntimeError" for h in tier.history), "witness: a publication did fail")
        self.assertGreaterEqual(len(tier.history), 6, "the loop kept probing after the failure")
        self.assertEqual(tier.tier(), "basic", "and still followed the tier afterwards")
        self.assertFalse(tier._thread.is_alive())

    def test_invalid_intervals_are_refused(self):
        with self.assertRaises(ValueError):
            ReprobingTier(ScriptedEs({"up": True, "type": "basic", "status": "active"}), interval_s=0)


class ServiceWiring(unittest.TestCase):
    def test_a_tier_change_rewrites_capabilities_json_atomically(self):
        node = {"up": True, "type": "basic", "status": "active"}
        a = mock.Mock(es="http://x", es_auth_file="f", es_ca=None, tenant="t1", ingest=None, logical="events", dims=8)
        with mock.patch("memcore.adapters.elasticsearch_search.ElasticsearchSearch", lambda *x, **k: ScriptedEs(node)):
            store, rep = basic_service.build(a)
        out = tempfile.mkdtemp()
        clk = Clock()
        with mock.patch("memcore.reprobe.ReprobingTier.start", lambda self, poll_s=0.2: None):
            tier = basic_service.start_reprobe(args(out=out), store, rep, stderr=open(os.devnull, "w"), clock=clk)
        node["type"] = "trial"
        clk.t += 61
        tier.maybe_refresh()
        with open(os.path.join(out, "capabilities.json")) as fh:
            self.assertEqual(json.load(fh)["tier"], "enterprise")
        self.assertEqual(sorted(os.listdir(out)), ["capabilities.json"], "no temp file left behind")

    def test_no_elasticsearch_means_no_tracker(self):
        self.assertIsNone(basic_service.start_reprobe(args(), object(), None))

    def test_the_cli_flag_exists_with_a_default_that_re_probes(self):
        ns = basic_service.parser().parse_args(["--out", "d"])
        self.assertEqual(ns.reprobe_interval_s, 60.0)


if __name__ == "__main__":
    unittest.main()
