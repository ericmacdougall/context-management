import os
import tempfile
import threading
import time
import unittest

from phasef.llm import LLM, CallCapExceeded, request_hash


def fake_runner(counter):
    def run(model, system, prompt):
        counter.append(prompt)
        return {"result": "ans:" + prompt, "usage": {"input_tokens": 5, "output_tokens": 2,
                "cache_creation_input_tokens": 7, "cache_read_input_tokens": 1}, "total_cost_usd": 0.01}
    return run


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.calls = []

    def mk(self, **kw):
        return LLM(os.path.join(self.d, "c"), os.path.join(self.d, "led.jsonl"), runner=fake_runner(self.calls), **kw)

    def test_cache_hit_costs_nothing(self):
        m = self.mk(cap=5)
        a = m.call("p", "s", "sonnet", "ORACLE", "q1", "read")
        b = m.call("p", "s", "sonnet", "ORACLE", "q1", "read")
        self.assertFalse(a["cached"])
        self.assertTrue(b["cached"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(m.calls_used(), 1)
        self.assertEqual(b["result"], "ans:p")

    def test_key_separates_model_recipe_system(self):
        self.assertEqual(len({request_hash("a", "v1", "s", "p"), request_hash("b", "v1", "s", "p"),
                              request_hash("a", "v2", "s", "p"), request_hash("a", "v1", "t", "p"),
                              request_hash("a", "v1", "s", "q")}), 5)
        m = self.mk(cap=5)
        m.call("p", "s", "sonnet", "A", "q", "read")
        m.call("p", "s", "opus", "A", "q", "read")
        self.assertEqual(len(self.calls), 2)

    def test_cap_refuses_and_does_not_run(self):
        m = self.mk(cap=2)
        m.call("1", "s", "sonnet", "A", "q", "read")
        m.call("2", "s", "sonnet", "A", "q", "read")
        with self.assertRaises(CallCapExceeded):
            m.call("3", "s", "sonnet", "A", "q", "read")
        self.assertEqual(len(self.calls), 2)
        m.call("1", "s", "sonnet", "A", "q", "read")  # cached still served at the cap

    def test_cap_persists_across_instances(self):
        self.mk(cap=1).call("1", "s", "sonnet", "A", "q", "read")
        with self.assertRaises(CallCapExceeded):
            self.mk(cap=1).call("2", "s", "sonnet", "A", "q", "read")

    def test_failed_call_still_counts(self):
        def boom(model, system, prompt):
            raise RuntimeError("x")
        m = LLM(os.path.join(self.d, "c"), os.path.join(self.d, "led.jsonl"), cap=1, runner=boom)
        with self.assertRaises(RuntimeError):
            m.call("1", "s", "sonnet", "A", "q", "read")
        self.assertEqual(m.calls_used(), 1)

    def test_ledger_and_concurrency_bound(self):
        active, peak, lock = [0], [0], threading.Lock()

        def slow(model, system, prompt):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {"result": "r", "usage": {"input_tokens": 1, "output_tokens": 1}}
        m = LLM(os.path.join(self.d, "c"), os.path.join(self.d, "led.jsonl"), cap=50, concurrency=3, runner=slow)
        ts = [threading.Thread(target=m.call, args=(str(i), "s", "sonnet", "A", "q", "read")) for i in range(9)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLessEqual(peak[0], 3)
        self.assertEqual(m.calls_used(), 9)
        with open(os.path.join(self.d, "led.jsonl")) as fh:
            self.assertEqual(len(fh.readlines()), 9)

    def test_cap_exact_across_many_instances_and_threads(self):
        errs, lock = [], threading.Lock()

        def work(i):
            m = LLM(os.path.join(self.d, "c"), os.path.join(self.d, "led.jsonl"), cap=10, runner=fake_runner(self.calls))
            try:
                m.call("p%d" % i, "s", "sonnet", "A", "q", "read")
            except CallCapExceeded:
                with lock:
                    errs.append(i)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(25)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(len(errs), 15)
        self.assertEqual(self.mk(cap=10).calls_used(), 10)

    def test_same_request_concurrently_runs_once(self):
        m = self.mk(cap=10)
        ts = [threading.Thread(target=m.call, args=("same", "s", "sonnet", "A", "q", "read")) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
