import os
import tempfile
import unittest

from phasef.llm import LLM, PromptLeak, check_prompt


class GuardTests(unittest.TestCase):
    def mk(self, forbidden=None):
        d = tempfile.mkdtemp()
        self.calls = []
        return LLM(os.path.join(d, "c"), os.path.join(d, "l.jsonl"), cap=5, runner=lambda m, s, p: (self.calls.append(p), {"result": "ok", "usage": {}})[1], forbidden=forbidden)

    def test_guard_fires_for_real_id_patterns_and_never_calls_the_model(self):
        m = self.mk()
        for bad in ("### Session answer_c6fd8ebd | date", "see answer_88841f26_1 here", "x sharegpt_JXpIEYh_11 y", "ultrachat_327291", "answer_ultrachat_113156"):
            with self.assertRaises(PromptLeak):
                m.call(bad, "sys", "sonnet", "A", "q", "s")
        with self.assertRaises(PromptLeak):
            m.call("fine", "system mentions answer_ab12cd", "sonnet", "A", "q", "s")
        self.assertEqual(self.calls, [])
        self.assertEqual(m.calls_used(), 0)

    def test_guard_checks_known_real_ids_and_passes_opaque(self):
        m = self.mk(forbidden={"07741c44_1", "af4a2fd1"})
        with self.assertRaises(PromptLeak):
            m.call("Session 07741c44_1 date", "s", "sonnet", "A", "q", "s")
        with self.assertRaises(PromptLeak):
            m.call("round af4a2fd1:3", "s", "sonnet", "A", "q", "s")
        self.assertEqual(m.call("Session s0123456789ab round s0123456789ab:3 Answer: 5", "s", "sonnet", "A", "q", "s")["result"], "ok")

    def test_guard_runs_before_cache_lookup(self):
        m = self.mk()
        m.call("clean", "s", "sonnet", "A", "q", "s")
        with self.assertRaises(PromptLeak):
            check_prompt("answer_deadbeef")


if __name__ == "__main__":
    unittest.main()
