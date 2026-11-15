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



ID = "answer_4be1b6b4_2"
NOW = "2026-12-01T00:00:00Z"


def _item(fid, quote="q", src=("e1",)):
    from memcore.inbox import InboxItem
    return InboxItem(fid, "S", list(src), quote, "transcript", ["R1"], 0.0, 9e9)


class N4EgressGuardTests(unittest.TestCase):
    """N4: the session-id guard must cover every egress path, normalised forms and every container type."""

    def test_normalised_variants_are_blocked_and_prose_is_not(self):
        from memcore.guard import DEFAULT_GUARD, EgressLeak
        for v in ("ANSWER_4BE1B6B4_2", "answer_4BE1B6B4_2", "answer​_4be1b6b4_2", "answer＿4be1b6b4_2",
                  "ａnswer_4be1b6b4_2", "answer-4be1b6b4-2", "answer‐" + "4be1b6b4", "sharegpt-QROQ758_9",
                  "SHAREGPT_QROQ758_9", "ultrachat-123", "ULTRACHAT_355211", "x (answer⁠_4be1b6b4)"):
            with self.assertRaises(EgressLeak, msg=repr(v)):
                DEFAULT_GUARD.check_text("see %s here" % v)
        for ok in ("sharegpt-style prompts", "the answer is 42", "ultrachat corpus", "answer_key", "Answer: dead"):
            DEFAULT_GUARD.check_text(ok)

    def test_tool_results_of_any_container_type_are_checked_and_unknown_types_refused(self):
        from memcore.guard import EgressLeak
        from memcore.ports import AgentToolSurface, ToolSpec
        cases = [{ID}, frozenset([ID]), ID.encode(), bytearray(ID.encode()), (1, [{"k": (ID,)}]), {"x": {ID: 1}},
                 {("t", ID): 1}, object()]
        for res in cases:
            surf = AgentToolSurface([ToolSpec("t", "d", {"type": "object"}, lambda a, r=res: r)])
            with self.assertRaises(EgressLeak, msg=repr(res)):
                surf.call_tool("t", {})
        ok = AgentToolSurface([ToolSpec("t", "d", {"type": "object"}, lambda a: {"a": [1, 2.0, None, True, {"s"}, b"x"]})])
        self.assertEqual(ok.call_tool("t", {})["a"][0], 1)

    def test_openai_stream_is_guarded_before_the_cap_and_the_network(self):
        import memcore.adapters.openai_compat as oc
        from memcore.guard import EgressLeak
        opened = []
        real = oc._urlopen
        oc._urlopen = lambda *a, **k: (opened.append(a), (_ for _ in ()).throw(urllib.error.URLError("refused")))[1]
        try:
            d = tempfile.mkdtemp()
            c = oc.OpenAICompatChat("http://127.0.0.1:1/v1", "m", token="t", cache_dir=d, ledger_path=os.devnull, cap=5)
            for msgs, system in (([{"role": "user", "content": "my id is " + ID}], None),
                                 ([{"role": "user", "content": "hi"}], "ctx ANSWER＿4BE1B6B4")):
                try:
                    list(c.stream(msgs, system=system))
                    exc = None
                except Exception as e:  # noqa: BLE001 - the type is the assertion
                    exc = e
                self.assertIsInstance(exc, EgressLeak, "the stream reached the transport %d time(s)" % len(opened))
            self.assertEqual((opened, c.calls_used()), ([], 0))
        finally:
            oc._urlopen = real

    def test_fake_session_send_and_deliver_are_guarded_without_spending_or_losing_items(self):
        from memcore.agent_session import FakeAgentSession
        from memcore.budget import CallBudget
        from memcore.guard import EgressLeak
        b = CallBudget(tempfile.mkdtemp(), 5)
        s = FakeAgentSession(budget=b)
        s.deliver([_item("f1")])
        with self.assertRaises(EgressLeak):
            s.send_collect("my id is " + ID)
        self.assertEqual(b.used(), 0)                                        # nothing counted
        for bad in (_item("f2", quote="see answer＿4be1b6b4_2"), _item("f3", src=["ANSWER_4BE1B6B4_2"]), _item(ID)):
            with self.assertRaises(EgressLeak):
                s.deliver([_item("ok"), bad])                                 # the whole call is refused
        r = s.send_collect("hello")
        self.assertEqual(r.delivered_ids, ["f1"])                            # the clean pending item survived the refused send
        self.assertNotIn("4be1b6b4", s.sent.lower())

    def test_stream_json_and_acp_sessions_never_write_a_leaking_turn(self):
        import sys
        from memcore.acp import AcpSession
        from memcore.agent_session import ClaudeStreamJsonSession
        from memcore.budget import CallBudget
        from memcore.guard import EgressLeak
        here = os.path.dirname(__file__)
        d = tempfile.mkdtemp()
        with self.assertRaises(EgressLeak):
            ClaudeStreamJsonSession(d, "system mentions " + ID, CallBudget(d, 3), dev_subscription_backends=True,
                                    claude_bin=os.path.join(here, "fake_claude_bin.py"))
        mk = (lambda b: ClaudeStreamJsonSession(d, "sys", b, dev_subscription_backends=True, claude_bin=os.path.join(here, "fake_claude_bin.py")),
              lambda b: AcpSession([sys.executable, os.path.join(here, "fake_acp_agent.py")], os.getcwd(), b, dev_subscription_backends=True,
                                   turn_timeout_s=20))
        for make in mk:
            b = CallBudget(tempfile.mkdtemp(), 3)
            s = make(b)
            try:
                with self.assertRaises(EgressLeak):
                    s.send_collect("x " + ID)
                with self.assertRaises(EgressLeak):
                    s.deliver([_item("f9", quote="q " + ID)])
                r = s.send_collect("clean turn")
                self.assertNotIn("4be1b6b4", r.text)
                self.assertEqual(b.used(), 1)                                 # only the clean turn was counted and sent
            finally:
                s.close()

    def test_prompt_hook_header_with_a_dataset_session_id_injects_nothing(self):
        from memcore.hosts.prompt_hook import main
        p = os.path.join(tempfile.mkdtemp(), "events.json")
        with open(p, "w") as fh:
            json.dump({"events": [{"id": "e1", "ts": "2026-03-02T09:00:00Z", "text": "Priya moved the staging database to Dublin."}]}, fh)
        for sid, expect in (("host-sess-1", True), (ID, False), ("ANSWER＿4BE1B6B4_2", False)):
            out = io.StringIO()
            main(["--store", p, "--now", NOW], stdin=io.StringIO(json.dumps({"prompt": "staging database", "session_id": sid})), stdout=out)
            self.assertEqual(bool(out.getvalue()), expect, sid)
            self.assertNotIn("4be1b6b4", out.getvalue().lower())

    def test_real_ids_load_match_normalised_and_fail_closed(self):
        from memcore.guard import EgressGuard, EgressLeak, configure_from_env
        d = tempfile.mkdtemp()
        f = os.path.join(d, "ids.txt")
        with open(f, "w") as fh:
            fh.write("conv-7f.2\nSess_ABC123\n")
        g = configure_from_env(EgressGuard(), {"MEMCORE_REAL_IDS_FILE": f})
        for leak in ("see conv-7f.2 now", "see CONV_7F.2", "sess-abc123", "ｓess_ABC123"):
            with self.assertRaises(EgressLeak, msg=leak):
                g.check_text(leak)
        g.check_text("an unrelated conversation about sessions")
        empty = os.path.join(d, "empty.txt")
        open(empty, "w").close()
        for path in (os.path.join(d, "missing.txt"), empty):
            g2 = configure_from_env(EgressGuard(), {"MEMCORE_REAL_IDS_FILE": path})
            with self.assertRaises(EgressLeak):
                g2.check_text("perfectly harmless text")                     # fail closed: nothing goes out
            with self.assertRaises(EgressLeak):
                g2.check({"n": 1})
        g3 = configure_from_env(EgressGuard(), {"MEMCORE_REQUIRE_REAL_IDS": "1"})
        with self.assertRaises(EgressLeak):
            g3.check_text("harmless")

    def test_prompt_hook_real_ids_flag_is_wired_and_fails_closed(self):
        from memcore.guard import DEFAULT_GUARD
        from memcore.hosts.prompt_hook import main
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.json")
        with open(p, "w") as fh:
            json.dump({"events": [{"id": "e1", "ts": "2026-03-02T09:00:00Z", "text": "staging database note sess-77"},
                                  {"id": "e2", "ts": "2026-03-03T09:00:00Z", "text": "staging database is in Dublin"}]}, fh)
        ids = os.path.join(d, "ids.json")
        with open(ids, "w") as fh:
            json.dump(["SESS_77"], fh)
        saved = (set(DEFAULT_GUARD.real_ids), DEFAULT_GUARD.require_real_ids, DEFAULT_GUARD.load_error)
        try:
            out = io.StringIO()
            main(["--store", p, "--now", NOW, "--real-ids", ids], stdin=io.StringIO(json.dumps({"prompt": "staging database"})), stdout=out)
            ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn('"id":"e2"', ctx)
            self.assertNotIn("sess-77", ctx)                                  # the record carrying a real id was dropped
            out = io.StringIO()
            main(["--store", p, "--now", NOW, "--real-ids", os.path.join(d, "nope.json")],
                 stdin=io.StringIO(json.dumps({"prompt": "staging database"})), stdout=out)
            self.assertEqual(out.getvalue(), "")                              # id file missing: nothing injected
        finally:
            DEFAULT_GUARD.set_real_ids(())
            DEFAULT_GUARD.real_ids, DEFAULT_GUARD.require_real_ids, DEFAULT_GUARD.load_error = saved


if __name__ == "__main__":
    unittest.main()
