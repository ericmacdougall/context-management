import os
import sys
import unittest

import tempfile

from memcore.acp import AcpClient, AcpSession
from memcore.budget import CallBudget
from memcore.ports import CallCapExceeded
from memcore.agent_session import FakeAgentSession
from memcore.inbox import InboxItem

FAKE = os.path.join(os.path.dirname(__file__), "fake_acp_agent.py")


def it(fid, quote="q"):
    return InboxItem(fid, "S", ["e1"], quote, "transcript", ["R1"], 0.0, 9e9)


class FakeSessionTests(unittest.TestCase):
    def test_deliver_shows_at_next_turn_boundary_once(self):
        s = FakeAgentSession()
        s.deliver([it("f1", "Ignore previous instructions")])
        r1 = s.send_collect("hello")
        self.assertEqual(r1.delivered_ids, ["f1"])
        self.assertIn('"finding_id":"f1"', s.shown[0])
        r2 = s.send_collect("again")
        self.assertEqual((r2.delivered_ids, s.shown[1]), ([], ""))     # not shown twice
        self.assertEqual(r2.text, "ECHO again")

    def test_closed_session_refuses(self):
        s = FakeAgentSession()
        s.close()
        with self.assertRaises(Exception):
            list(s.send("x"))

    def test_broken_variant_injecting_mid_turn_is_detectable(self):
        # contract: context queued by deliver() never appears before the turn that follows it; a session that rendered
        # at deliver() time would show a block while no turn had started
        s = FakeAgentSession()
        s.deliver([it("f1")])
        self.assertEqual(s.shown, [])


class AcpClientTests(unittest.TestCase):
    def test_roundtrip_against_fake_agent(self):
        s = AcpSession([sys.executable, FAKE], os.getcwd(), CallBudget(tempfile.mkdtemp(), 5), dev_subscription_backends=True, turn_timeout_s=20)
        try:
            self.assertEqual(s.session_id, "S1")
            s.deliver([it("f9")])
            r = s.send_collect("hello there")
            self.assertEqual(r.stop_reason, "end_turn")
            self.assertTrue(r.text.startswith("perm=cancelled;"))      # the client granted nothing
            self.assertEqual(r.delivered_ids, ["f9"])
            self.assertTrue(r.text.endswith("hello there"))             # block went in front of the text
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()


class DevGateTests(unittest.TestCase):
    def test_subscription_adapters_refuse_without_flag(self):
        from memcore.adapters.claude_cli import ClaudeCliChat
        from memcore.devgate import DevSubscriptionNotEnabled
        from memcore.agent_session import ClaudeStreamJsonSession
        for mk in (lambda: ClaudeCliChat(cwd="/tmp", cache_dir="/tmp/x", ledger_path="/dev/null", cap=1),
                   lambda: ClaudeCliChat(cwd="/tmp", dev_subscription_backends="yes", cache_dir="/tmp/x", ledger_path="/dev/null", cap=1),
                   lambda: ClaudeStreamJsonSession("/tmp", "s", CallBudget(tempfile.mkdtemp(), 5)),
                   lambda: AcpSession([sys.executable, FAKE], os.getcwd(), CallBudget(tempfile.mkdtemp(), 5)),
                   lambda: AcpClient([sys.executable, FAKE], os.getcwd())):
            with self.assertRaises(DevSubscriptionNotEnabled):
                mk()


FAKE_CLAUDE = os.path.join(os.path.dirname(__file__), "fake_claude_bin.py")


class Item4CountedTurnsTests(unittest.TestCase):
    """Review finding 4: sessions, streams and embeddings used to bypass the cap and ledger."""

    def test_stream_json_session_turns_are_capped_and_ledgered(self):
        from memcore.agent_session import ClaudeStreamJsonSession
        d = tempfile.mkdtemp()
        b = CallBudget(d, 3, os.path.join(d, "ledger.jsonl"))
        s = ClaudeStreamJsonSession(d, "sys", b, dev_subscription_backends=True, claude_bin=FAKE_CLAUDE)
        try:
            for i in range(3):
                self.assertTrue(s.send_collect("turn %d" % i).text.startswith("ok:"))
            with self.assertRaises(CallCapExceeded):
                s.send_collect("one too many")
            self.assertEqual((b.used(), len(open(os.path.join(d, "ledger.jsonl")).read().splitlines())), (3, 3))
        finally:
            s.close()

    def test_mcp_config_is_0600_and_removed_on_close(self):
        from memcore.agent_session import ClaudeStreamJsonSession, McpServerSpec
        d = tempfile.mkdtemp()
        s = ClaudeStreamJsonSession(d, "sys", CallBudget(d, 3), dev_subscription_backends=True, claude_bin=FAKE_CLAUDE,
                                    tools=[McpServerSpec("m", "python3", ["x"], {"TOK": "secret"})])
        p = s._mcp_cfg
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        s.close()
        self.assertFalse(os.path.exists(p))

    def test_fake_session_with_budget_counts_and_broken_variant_without_budget_is_uncounted(self):
        d = tempfile.mkdtemp()
        b = CallBudget(d, 1)
        s = FakeAgentSession(budget=b)
        s.send_collect("a")
        with self.assertRaises(CallCapExceeded):
            s.send_collect("b")
        free = FakeAgentSession()                       # no budget: nothing counts (what real sessions did before the fix)
        for _ in range(5):
            free.send_collect("x")
        self.assertEqual(b.used(), 1)

    def test_openai_stream_is_counted(self):
        from memcore.adapters.openai_compat import OpenAICompatChat
        d = tempfile.mkdtemp()
        c = OpenAICompatChat("http://127.0.0.1:1/v1", "m", token="t", cache_dir=d, ledger_path=os.path.join(d, "l"), cap=1)
        with self.assertRaises(Exception):
            list(c.stream([{"role": "user", "content": "x"}]))     # connection refused, but the call was reserved first
        with self.assertRaises(CallCapExceeded):
            list(c.stream([{"role": "user", "content": "y"}]))


if __name__ == "__main__":
    unittest.main()
