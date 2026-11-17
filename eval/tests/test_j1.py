"""Stage 11 J1 tests: selection rule, parsers, scorer withholding, and broken implementations that must fail for the stated reason."""
import json
import unittest

from pilots import j1_items as J
from pilots import j1_score as S
from pilots.llm import LLM, LimitStop, ModelMismatch, request_hash

BARS = {"fa_upper_max": 0.10, "fr_upper_max": 0.15, "agree_lower_min": 0.80, "control_rejected_min": 18}
TYPES = ["multi-session", "temporal-reasoning", "knowledge-update", "single-session-user", "single-session-preference"]


def rows(n=94):
    return [{"qid": "q%03d" % i, "qtype": TYPES[i % 5], "question": "question %d?" % i, "answer": "gold %d" % i, "question_date": "2023-05-01 10:00", "cluster": "c%d" % (i % 90)} for i in range(n)]


def gens(rs):
    g = {}
    for r in rs:
        d = {k: "Reasoning %s.\nAnswer: x %s" % (k, r["qid"]) for k in ("wrong_value", "near_miss_wrong", "para_terse", "para_verbose")}
        for k in J.kinds_for(r["qtype"]):
            d["border:" + k] = "Reasoning.\nAnswer: %s %s" % (k, r["qid"])
        g[r["qid"]] = d
    return g


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.rs = rows()
        self.items = J.select_items(self.rs, gens(self.rs))

    def test_sizes_and_distinct_clusters(self):
        for c, n in (("wrong", 60), ("paraphrase", 60), ("borderline", 40)):
            xs = [i for i in self.items if i["category"] == c]
            self.assertEqual(len(xs), n)
            self.assertEqual(len({i["cluster"] for i in xs}), n)

    def test_borderline_kind_cap(self):
        cnt = {}
        for i in self.items:
            if i["category"] == "borderline":
                cnt[i["kind"]] = cnt.get(i["kind"], 0) + 1
        self.assertTrue(max(cnt.values()) <= J.BORDERLINE_CAP)

    def test_deterministic(self):
        again = J.select_items(self.rs, gens(self.rs))
        self.assertEqual([i["item_id"] for i in again], [i["item_id"] for i in self.items])

    def test_control_uses_foreign_gold(self):
        ctl = J.control_items(self.rs, self.items)
        self.assertEqual(len(ctl), 20)
        by = {r["qid"]: r for r in self.rs}
        for c in ctl:
            self.assertNotEqual(c["gold"], by[c["qid"]]["answer"])
            self.assertEqual(by[c["foreign_gold_from"]]["qtype"], c["qtype"])
            self.assertNotEqual(by[c["foreign_gold_from"]]["cluster"], c["cluster"])

    def test_invalid_generation_is_skipped(self):
        g = gens(self.rs)
        g["q000"] = {}
        items = J.select_items(self.rs, g)
        self.assertFalse(any(i["qid"] == "q000" and i["bucket"] in ("wrong_value", "para_terse") for i in items))


class TestParse(unittest.TestCase):
    def test_gen_parse(self):
        txt = json.dumps({"wrong_value": "no", "near_miss_wrong": "Good.\nAnswer: A", "para_terse": "Yes it is.\nAnswer: B", "para_verbose": "x" * 30 + "\nAnswer: C",
                          "borderline": {"hedge_two": "Maybe.\nAnswer: A or B", "partial": None}})
        d = J.parse_gen("```json\n" + txt + "\n```", "multi-session")
        self.assertNotIn("wrong_value", d)  # too short and no Answer line
        self.assertEqual(J.parse_gen(json.dumps({"para_terse": "A fine sentence here. Answer: B"}), "multi-session")["para_terse"], "A fine sentence here.\nAnswer: B")
        self.assertIn("near_miss_wrong", d)
        self.assertIn("border:hedge_two", d)
        self.assertNotIn("border:partial", d)

    def test_verdict_needs_exactly_one_line(self):
        self.assertTrue(J.parse_verdict("Fine.\nVerdict: yes"))
        self.assertFalse(J.parse_verdict("Verdict: no"))
        with self.assertRaises(ValueError):
            J.parse_verdict("yes")
        with self.assertRaises(ValueError):
            J.parse_verdict("Verdict: yes\nVerdict: no")


def mk(items, judge_fn, audit_fn, ctl_fn=lambda i: False, model=("claude-opus-5-5",)):
    jr = [{"item_id": i["item_id"], "verdict": judge_fn(i), "model_returned": list(model)} for i in items]
    ar = [{"item_id": i["item_id"], "verdict": audit_fn(i)} for i in items if i["category"] == "borderline"]
    cr = [{"item_id": "control:%d" % n, "verdict": ctl_fn(n), "model_returned": list(model)} for n in range(20)]
    return jr, cr, ar


class TestScore(unittest.TestCase):
    def setUp(self):
        rs = rows()
        self.items = J.select_items(rs, gens(rs))
        self.perfect = lambda i: i["construction_label"] == "yes" if i["category"] != "borderline" else i["kind"] in ("off_by_one", "stale_and_updated")

    def run_score(self, judge_fn, audit_fn=None, **kw):
        audit_fn = audit_fn or judge_fn
        jr, cr, ar = mk(self.items, judge_fn, audit_fn, **kw)
        return S.score(self.items, jr, cr, ar, BARS)

    def test_ideal_judge_passes(self):
        r = self.run_score(self.perfect)
        self.assertEqual(r["status"], "SCORED")
        self.assertTrue(r["g_judge_pass"])

    def test_bounds_math(self):
        self.assertAlmostEqual(S.wilson(1, 60)[1], 0.089, places=3)
        self.assertGreater(S.wilson(2, 60)[1], 0.10)
        self.assertGreater(S.wilson(4, 60)[1], 0.15)
        self.assertLess(S.wilson(36, 40)[0], 0.80)
        self.assertGreaterEqual(S.wilson(37, 40)[0], 0.80)

    def test_always_yes_fails_leniency_for_the_stated_reason(self):
        r = self.run_score(lambda i: True, audit_fn=self.perfect)
        self.assertFalse(r["wrong_false_accept"]["pass"])
        self.assertTrue(r["correct_false_reject"]["pass"])
        self.assertFalse(r["g_judge_pass"])

    def test_always_no_fails_strictness_for_the_stated_reason(self):
        r = self.run_score(lambda i: False, audit_fn=self.perfect)
        self.assertTrue(r["wrong_false_accept"]["pass"])
        self.assertFalse(r["correct_false_reject"]["pass"])

    def test_judge_that_ignores_gold_fails_the_control(self):
        r = self.run_score(self.perfect, ctl_fn=lambda n: True)
        self.assertFalse(r["control_shuffled_gold"]["pass"])
        self.assertFalse(r["g_judge_pass"])

    def test_audit_disagreement_fails_agreement(self):
        r = self.run_score(self.perfect, audit_fn=lambda i: (not self.perfect(i)) if i["category"] == "borderline" else True)
        self.assertFalse(r["audit_agreement"]["pass"])

    def test_withheld_on_missing_row(self):
        jr, cr, ar = mk(self.items, self.perfect, self.perfect)
        self.assertEqual(S.score(self.items, jr[1:], cr, ar, BARS)["status"], "WITHHELD")

    def test_withheld_on_model_mismatch(self):
        jr, cr, ar = mk(self.items, self.perfect, self.perfect, model=("claude-sonnet-5-5",))
        self.assertEqual(S.score(self.items, jr, cr, ar, BARS)["status"], "WITHHELD")

    def test_withheld_on_missing_audit_or_control(self):
        jr, cr, ar = mk(self.items, self.perfect, self.perfect)
        self.assertEqual(S.score(self.items, jr, cr, ar[1:], BARS)["status"], "WITHHELD")
        self.assertEqual(S.score(self.items, jr, cr[1:], ar, BARS)["status"], "WITHHELD")

    def test_withheld_on_duplicate_row(self):
        jr, cr, ar = mk(self.items, self.perfect, self.perfect)
        self.assertEqual(S.score(self.items, jr + jr[:1], cr, ar, BARS)["status"], "WITHHELD")


class TestWrapper(unittest.TestCase):
    def mk(self, tmp, runner, cap=3, backend="claude"):
        return LLM(tmp + "/cache", tmp + "/run", cap, recipe="t", runner=runner, backend=backend)

    def ok(self, model="claude-opus-5-5"):
        return lambda m, s, p: {"result": "1: yes", "model_returned": [model], "in_tokens": 1, "cache_creation": 0, "cache_read": 0, "out_tokens": 1, "cost_usd": None, "num_turns": 1}

    def test_cache_hit_costs_nothing_and_cap_holds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            m = self.mk(d, self.ok(), cap=1)
            a = m.call("p", "s", "claude-opus-5-5", "t", "q", "st")
            b = m.call("p", "s", "claude-opus-5-5", "t", "q", "st")
            self.assertFalse(a["cached"])
            self.assertTrue(b["cached"])
            self.assertEqual(m.calls_used(), 1)
            from pilots.llm import CallCapExceeded
            with self.assertRaises(CallCapExceeded):
                m.call("p2", "s", "claude-opus-5-5", "t", "q", "st")

    def test_model_mismatch_withheld_and_not_cached(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            m = self.mk(d, self.ok("claude-haiku-4-5"))
            with self.assertRaises(ModelMismatch):
                m.call("p", "s", "claude-opus-5-5", "t", "q", "st")
            self.assertFalse(os.path.exists(d + "/cache/" + request_hash("claude-opus-5-5", "t", "s", "p") + ".json"))

    def test_limit_stops_run_and_nothing_else_is_called(self):
        import os
        import tempfile

        def boom(m, s, p):
            raise LimitStop("usage limit reached")
        with tempfile.TemporaryDirectory() as d:
            m = self.mk(d, boom)
            with self.assertRaises(LimitStop):
                m.call("p", "s", "claude-opus-5-5", "t", "q", "st")
            self.assertTrue(os.path.exists(d + "/run/STOPPED.json"))
            m2 = self.mk(d, self.ok())
            with self.assertRaises(LimitStop):
                m2.call("other", "s", "claude-opus-5-5", "t", "q", "st")


if __name__ == "__main__":
    unittest.main()
