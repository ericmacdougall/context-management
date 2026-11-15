import os
import sys
import unittest

from memcore.acp import AcpSession
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
        s = AcpSession([sys.executable, FAKE], os.getcwd(), dev_subscription_backends=True, turn_timeout_s=20)
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
                   lambda: ClaudeStreamJsonSession("/tmp", "s"),
                   lambda: AcpSession([sys.executable, FAKE], os.getcwd())):
            with self.assertRaises(DevSubscriptionNotEnabled):
                mk()
