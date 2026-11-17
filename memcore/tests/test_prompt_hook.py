import io
import json
import os
import tempfile
import unittest

from memcore.hosts.prompt_hook import DEFAULT_MAX, build_output, main
from memcore.memtools import BudgetedInjector, InMemoryStore

NOW = "2026-12-01T00:00:00Z"


def store(n=1, text="Priya moved the staging database to Dublin."):
    s = InMemoryStore()
    for i in range(n):
        s.add_event("ev%d" % i, "s", "2026-03-%02dT09:00:00Z" % (1 + i % 28), text + " staging database " * 20)
    return s


from memcore.tests._realids import hook_main

class HookTests(unittest.TestCase):
    def test_output_shape_for_both_hosts(self):
        out = build_output(BudgetedInjector(store()), {"session_id": "x", "prompt": "where is the staging database", "hook_event_name": "UserPromptSubmit"}, NOW)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("ev0", out["hookSpecificOutput"]["additionalContext"])

    def test_no_match_means_no_output(self):
        self.assertIsNone(build_output(BudgetedInjector(store()), {"prompt": "zzzz qqqq"}, NOW))

    def test_cap_respected_even_with_big_budget(self):
        inj = BudgetedInjector(store(40), char_budget=50000, k=40)
        out = build_output(inj, {"prompt": "staging database"}, NOW, DEFAULT_MAX)
        self.assertLessEqual(len(out["hookSpecificOutput"]["additionalContext"]), 10000)
        self.assertLessEqual(len(out["hookSpecificOutput"]["additionalContext"]), DEFAULT_MAX)

    def test_broken_variant_without_own_cap_exceeds_host_limit(self):
        inj = BudgetedInjector(store(40), char_budget=50000, k=40)
        raw = inj.build(__import__("memcore.ports", fromlist=["Turn"]).Turn("s", "staging database", NOW)).block
        self.assertGreater(len(raw), 10000)   # what would have been sent without the adapter's cap

    def test_failure_is_silent_exit_zero(self):
        err = io.StringIO()
        out = io.StringIO()
        rc = hook_main(["--store", "/nonexistent.json"], stdin=io.StringIO("{}"), stdout=out)
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_main_end_to_end(self):
        p = os.path.join(tempfile.mkdtemp(), "events.json")
        json.dump({"events": [{"id": "e1", "ts": "2026-03-02T09:00:00Z", "text": "Priya moved the staging database to Dublin."}]}, open(p, "w"))
        out = io.StringIO()
        hook_main(["--store", p, "--now", NOW], stdin=io.StringIO(json.dumps({"prompt": "staging database"})), stdout=out)
        self.assertIn("e1", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
