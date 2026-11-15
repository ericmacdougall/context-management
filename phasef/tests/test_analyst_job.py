import json
import os
import tempfile
import unittest

from phasef.analyst_job import plan, run, parse
from phasef.analyst import result_path

Q = ["q1", "q2", "q3"]
LOW = ["q1", "q2"]


def runner_factory(counter, bad=False):
    def run_(model, system, prompt):
        counter.append(prompt)
        if "rank" in system.lower() or "ranking" in prompt.lower():
            ids = [l[1:l.index("]")] for l in prompt.splitlines() if l.startswith("[") and "]" in l]
            return {"result": json.dumps({"ranking": ids if not bad else ["zzz"]}), "usage": {"input_tokens": 1, "output_tokens": 1}}
        ids = [l[1:l.index("]")] for l in prompt.splitlines() if l.startswith("[") and "]" in l][:2]
        return {"result": json.dumps({"results": [{"round_id": i, "reason": "r"} for i in ids]}), "usage": {"input_tokens": 1, "output_tokens": 1}}
    return run_


class JobTests(unittest.TestCase):
    def setup_dirs(self):
        d = tempfile.mkdtemp()
        for lv in ("W", "H1"):
            os.makedirs(os.path.join(d, "tasks", lv))
            for q in Q:
                json.dump({"question": "What is thing number %d?" % (Q.index(q) + 7), "question_date": "2023/05/30 (Tue) 12:18", "_meta": {"question_id": q, "level": lv},
                           "candidates": [{"round_id": "s0123456789ab:%d" % i, "date": "2023/01/0%d" % (i + 1), "text": "text %d" % i} for i in range(3)]},
                          open(os.path.join(d, "tasks", lv, q + ".json"), "w"))
        json.dump(Q, open(os.path.join(d, "q.json"), "w"))
        json.dump(LOW, open(os.path.join(d, "low.json"), "w"))
        return d

    def args(self, d, phase, extra=()):
        a = parse(["--phase", phase, "--tasks-dir", os.path.join(d, "tasks"), "--backend", "true", "--questions-file", os.path.join(d, "q.json"), "--lowoverlap-ids", os.path.join(d, "low.json"),
                   "--canary-level", "W", "--levels", "H1", "--secondary-level", "H1", "--work", os.path.join(d, "work"), "--out", os.path.join(d, "out"), "--remote", "",
                   "--stop-file", os.path.join(d, "STOP")] + list(extra))
        return a

    def test_plan_order_and_scope(self):
        self.assertEqual(plan("canary", Q, LOW, ["H1"], "W", None, "ANALYST"), [("ANALYST-RERANK-ONLY", "W", "q1"), ("ANALYST", "W", "q1"), ("ANALYST-RERANK-ONLY", "W", "q2"), ("ANALYST", "W", "q2")])
        rest = plan("rest", Q, LOW, ["H1"], "W", None, "ANALYST")
        self.assertEqual(len(rest), 6)
        self.assertEqual(plan("secondary", Q, LOW, ["H1"], "W", "H1", "ANALYST"), [("ANALYST", "H1", "q1"), ("ANALYST", "H1", "q2")])

    def test_canary_runs_writes_final_layout_resumes_and_honours_stop_file(self):
        d = self.setup_dirs()
        calls = []
        a = self.args(d, "canary")
        # rerank only (analyst arm needs the real CLI); patch ARMS by running rerank items through the injected runner
        import phasef.analyst_job as J
        orig = J.ARMS
        J.ARMS = ("ANALYST-RERANK-ONLY",)
        try:
            st = run(a, runner_factory(calls))
            self.assertEqual(st, {"ok": 2, "failed": 0})
            self.assertTrue(os.path.exists(result_path(a.out, "ANALYST-RERANK-ONLY", "W", "q1")))
            n = len(calls)
            run(a, runner_factory(calls))
            self.assertEqual(len(calls), n)  # resumable: finished items are skipped
            open(a.stop_file, "w").write("x")
            a2 = self.args(d, "rest")
            run(a2, runner_factory(calls))
            self.assertFalse(os.path.exists(result_path(a2.out, "ANALYST-RERANK-ONLY", "H1", "q1")))  # stop file ends the loop
        finally:
            J.ARMS = orig

    def test_rejected_result_is_recorded_not_retried(self):
        d = self.setup_dirs()
        calls = []
        import phasef.analyst_job as J
        orig = J.ARMS
        J.ARMS = ("ANALYST-RERANK-ONLY",)
        try:
            a = self.args(d, "canary")
            st = run(a, runner_factory(calls, bad=True))
            self.assertEqual(st, {"ok": 0, "failed": 2})
            self.assertEqual(len(calls), 2)  # one call per item, no retry
            rec = json.load(open(result_path(a.out, "ANALYST-RERANK-ONLY", "W", "q1")))
            self.assertEqual(rec["status"], "rejected")
        finally:
            J.ARMS = orig


if __name__ == "__main__":
    unittest.main()
