import os
import unittest

from memcore.adapters.codex_cli import CodexCliChat
from memcore.tests.contract import ChatModelContract, OUT


class CodexCliContract(ChatModelContract, unittest.TestCase):
    NAME = "codex-cli"
    SKIP_OVERLONG = True  # context limit unknown to the adapter (declared None); documented, not silently passed
    TIMEOUT_S = 1.0       # the pool clamps --max-seconds to >=30 but our own kill fires at 1s

    def make_model(self):
        return CodexCliChat(cwd=os.path.join(OUT, "neutral"), cache_dir=os.path.join(OUT, "cache", "codex-cli"),
                            ledger_path=os.path.join(OUT, "ledger.jsonl"), cap=80, timeout_s=300)


if __name__ == "__main__":
    unittest.main()
