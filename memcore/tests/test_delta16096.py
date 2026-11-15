"""Regression tests for the delta review of 888c94b (goodqa#16096): N1, N2, N4, N5, N7 and the Kibana pre-execution template.
Each test exercises the real call path and fails on d8f714b for the reason the review gives (new symbols are imported inside
the tests, so the old tree reports each test separately). Stubs and loopback sockets only; no model call."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import memcore.gateway as gw_mod
from memcore.fakes import FakeChat

BRIDGE = "172.17.0.1"


class _NoBind:
    """Stands in for ThreadingHTTPServer: the bridge address does not exist on a dev Mac, so the gate is tested without a bind."""
    daemon_threads = True

    def __init__(self, addr, handler):
        self.server_address, self.RequestHandlerClass = addr, handler

    def server_close(self):
        pass


class _SubscriptionBackend:          # stands in for ClaudeCliChat
    model, subscription_backed = "claude", True

    def complete(self, **k):
        raise AssertionError("a subscription call was made")


def _post(base, tok, body):
    r = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                               headers={"Authorization": "Bearer " + tok})
    return urllib.request.urlopen(r, timeout=10)


class N1BridgeAllowListTests(unittest.TestCase):
    """N1: network safety must not be self-declared by a class whose behaviour is caller code."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._real = gw_mod.ThreadingHTTPServer
        gw_mod.ThreadingHTTPServer = _NoBind

    def tearDown(self):
        gw_mod.ThreadingHTTPServer = self._real

    def gw(self, b, **kw):
        return gw_mod.Gateway(b, tempfile.mkdtemp(dir=self.d), host=BRIDGE, allow_bridge_host=BRIDGE, **kw)

    def fake(self, fn, cls=FakeChat):
        return cls(fn, cache_dir=tempfile.mkdtemp(dir=self.d), ledger_path=os.devnull, cap=5)

    def test_fakechat_forwarding_to_a_subscription_is_refused_on_the_bridge(self):
        sub = _SubscriptionBackend()
        with self.assertRaises(ValueError):
            self.gw(self.fake(lambda p, s, j: sub.complete()))

    def test_fakechat_subclass_router_is_refused_on_the_bridge(self):
        class Router(FakeChat):
            pass
        with self.assertRaises(ValueError):
            self.gw(self.fake(lambda p, s, j: "x", Router))
        with self.assertRaises(ValueError):
            self.gw({"a": self.fake(lambda p, s, j: "x", Router)})

    def stub(self, **kw):
        from memcore.fakes import ScriptedStubChat
        return ScriptedStubChat(cache_dir=tempfile.mkdtemp(dir=self.d), ledger_path=os.devnull, cap=5, **kw)

    def test_allow_listed_data_only_stub_is_accepted_on_the_bridge(self):
        g = self.gw(self.stub(rules=[("staging", "Dublin")]))           # the allowed path is asserted, not just the refusals
        self.assertEqual((g.host, g.port), (BRIDGE, 0))
        g2 = self.gw({"s1": self.stub(), "s2": self.stub(model="stub-2")})
        self.assertEqual(g2.host, BRIDGE)

    def test_stub_with_injected_behaviour_or_subclass_is_refused(self):
        from memcore.fakes import ScriptedStubChat
        sub = _SubscriptionBackend()
        s = self.stub()
        s._call_backend = lambda *a, **k: sub.complete()                   # instance-level override: caller code again
        with self.assertRaises(ValueError):
            self.gw(s)
        s2 = self.stub()
        s2.complete = lambda **k: sub.complete()
        with self.assertRaises(ValueError):
            self.gw(s2)
        s3 = self.stub()
        s3.rules = (("x", sub),)                                           # rules must stay plain strings
        with self.assertRaises(ValueError):
            self.gw(s3)

        class Sub(ScriptedStubChat):
            pass
        with self.assertRaises(ValueError):
            self.gw(Sub(cache_dir=tempfile.mkdtemp(dir=self.d), ledger_path=os.devnull, cap=5))
        with self.assertRaises(TypeError):
            ScriptedStubChat(cache_dir=tempfile.mkdtemp(dir=self.d), ledger_path=os.devnull, cap=5, rules=[("x", lambda: 1)])

    def test_tainted_stub_is_refused_even_with_the_dev_flag(self):
        s = self.stub()
        s.subscription_backed = True
        with self.assertRaises(ValueError):
            self.gw(s, dev_subscription_backends=True)


class N2SnapshotTests(unittest.TestCase):
    """N2: the gate must cover exactly what the handler serves, for the gateway's whole life."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fc = FakeChat(lambda p, s, j: "stub", cache_dir=os.path.join(self.d, "c"), ledger_path=os.devnull, cap=5)
        self.backs = {"fake-1": self.fc}
        self.g = gw_mod.Gateway(self.backs, os.path.join(self.d, "g"))       # loopback, no dev flag
        self.g.start()
        self.tok = open(os.path.join(self.d, "g", "gateway.token")).read().strip()

    def tearDown(self):
        self.g.shutdown()

    def test_backend_added_to_the_callers_dict_after_construction_is_not_served(self):
        tainted = FakeChat(lambda p, s, j: "SUBSCRIPTION ANSWER", cache_dir=os.path.join(self.d, "c2"), ledger_path=os.devnull, cap=5)
        tainted.subscription_backed = True
        self.backs["claude"] = tainted
        with self.assertRaises(urllib.error.HTTPError) as c:
            _post(self.g.base_url, self.tok, {"model": "claude", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(c.exception.code, 404)
        self.assertEqual(tainted.backend_calls, 0)

    def test_backend_tainted_after_construction_is_refused_per_request(self):
        r = json.load(_post(self.g.base_url, self.tok, {"model": "fake-1", "messages": [{"role": "user", "content": "one"}]}))
        self.assertEqual(r["choices"][0]["message"]["content"], "stub")       # control: served before the mutation
        self.fc.subscription_backed = True
        with self.assertRaises(urllib.error.HTTPError) as c:
            _post(self.g.base_url, self.tok, {"model": "fake-1", "messages": [{"role": "user", "content": "two"}]})
        self.assertEqual(c.exception.code, 503)
        self.assertEqual(self.fc.backend_calls, 1)


if __name__ == "__main__":
    unittest.main()
