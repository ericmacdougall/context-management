"""Contract suite run unchanged through client -> gateway -> claude-cli (shares the claude cache and call cap)."""
import os
import unittest

from memcore.adapters.claude_cli import ClaudeCliChat
from memcore.adapters.openai_compat import OpenAICompatChat
from memcore.gateway import Gateway
from memcore.tests.contract import ChatModelContract, OUT

_gw = None


def setUpModule():
    global _gw
    backend = ClaudeCliChat(dev_subscription_backends=True, cwd=os.path.join(OUT, "neutral"), model="sonnet", cache_dir=os.path.join(OUT, "cache", "claude-cli"),
                            ledger_path=os.path.join(OUT, "ledger.jsonl"), cap=200, concurrency=3, timeout_s=300)
    _gw = Gateway(backend, os.path.join(OUT, "gw-test"), dev_subscription_backends=True)
    _gw.start()


def tearDownModule():
    _gw.shutdown()


class GatewayClaudeContract(ChatModelContract, unittest.TestCase):
    NAME = "gateway->claude-cli"

    def make_model(self):
        return OpenAICompatChat(_gw.base_url, "sonnet", token=_gw.token, cache_dir=os.path.join(OUT, "cache", "openai-via-gateway"),
                                ledger_path=os.path.join(OUT, "ledger-client.jsonl"), cap=1000, concurrency=3,
                                max_context_tokens=200_000, timeout_s=300)


if __name__ == "__main__":
    unittest.main()
