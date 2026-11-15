import os
import unittest

from memcore.adapters.claude_cli import ClaudeCliChat
from memcore.tests.contract import ChatModelContract, OUT


class ClaudeCliContract(ChatModelContract, unittest.TestCase):
    NAME = "claude-cli"

    def make_model(self):
        return ClaudeCliChat(cwd=os.path.join(OUT, "neutral"), model="sonnet", cache_dir=os.path.join(OUT, "cache", "claude-cli"),
                             ledger_path=os.path.join(OUT, "ledger.jsonl"), cap=200, concurrency=3, timeout_s=300)


if __name__ == "__main__":
    unittest.main()
