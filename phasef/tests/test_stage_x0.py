import json
import os
import tempfile
import unittest

from phasef import stage_x0 as X
from phasef.anon import opaque_sid

TURNS = [{"role": "user", "content": "My cat is Luna."}, {"role": "assistant", "content": "Nice name."}, {"role": "user", "content": "I live in Oslo."}, {"role": "assistant", "content": "Lovely."}]
SID = "s0123456789ab"
DATE = "2023/05/20 (Sat) 02:21"


def runner_for(text, calls=None):
    def run(model, system, prompt):
        if calls is not None:
            calls.append(prompt)
        return {"result": text, "usage": {"input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 11000, "cache_read_input_tokens": 940}, "total_cost_usd": 0.08}
    return run


GOOD = json.dumps({"sessions": [{"session_id": SID, "facts": [
    {"subject": "user", "slot": "user.cat_name", "predicate": "has cat named", "object": "Luna", "cardinality": "single", "round": 0, "quote": "My cat is Luna.", "author": "user", "cue_keys": ["pet"]},
    {"subject": "user", "slot": "u.x", "predicate": "p"}]}]})


class Args:
    pass


class X0(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = X.X
        X.X = self.d
        os.makedirs(os.path.join(self.d, "sessions"))
        json.dump({"session_id": SID, "date": DATE, "turns": TURNS}, open(os.path.join(self.d, "sessions", SID + ".json"), "w"))
        self.a = Args()

    def tearDown(self):
        X.X = self.old

    def test_prompt_unchanged_and_id_not_hashed_again(self):
        self.assertEqual(X.SHA16, "a2336c7976bb8fc2")
        p = X.build_prompt(SID, DATE, TURNS)
        self.assertIn("### SESSION id=%s date=%s\n[r0] user: My cat is Luna.\nassistant: Nice name.\n[r1] user: I live in Oslo.\nassistant: Lovely." % (SID, DATE), p)
        self.assertNotIn(opaque_sid(SID), p)
        self.assertIn("Below are 1 chat sessions", p)

    def test_parse_statuses(self):
        facts, dropped, repaired = X.parse_one(GOOD, SID)
        self.assertEqual((len(facts), dropped, repaired, facts[0]["round_id"], facts[0]["session_id"]), (1, 1, False, SID + ":0", SID))
        cut = GOOD[:-2] + "</parameter>"
        self.assertTrue(X.parse_one(cut, SID)[2])
        with self.assertRaises(ValueError):
            X.parse_one("no json here", SID)
        with self.assertRaises(ValueError):
            X.parse_one(GOOD.replace(SID, "sOTHER"), SID)

    def test_record_fields_and_parse_failure_and_call_error(self):
        m = X.Meter()
        rec = X.run_one(0, SID, self.a, m, runner_for(GOOD))
        self.assertEqual(rec["status"], "ok")
        for k in ("session_id", "order_index", "status", "reject_reason", "prompt_version", "prompt_sha16", "model", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "seconds", "cost_usd", "dropped", "facts", "raw_output_path"):
            self.assertIn(k, rec)
        self.assertEqual((rec["input_tokens"], rec["cache_creation_input_tokens"], rec["cost_usd"], rec["session_rounds"], rec["n_facts"]), (3, 11000, 0.08, 2, 1))
        self.assertEqual(m.total, 3 + 11000 + 940)
        for sid2, runner, want in (("s1111111111aa", runner_for("garbage"), "parse_failure"), ("s2222222222bb", None, "call_error")):
            json.dump({"session_id": sid2, "date": DATE, "turns": TURNS + [{"role": "user", "content": sid2}, {"role": "assistant", "content": "x"}]}, open(os.path.join(self.d, "sessions", sid2 + ".json"), "w"))
            if runner is None:
                def runner(model, system, prompt):
                    raise RuntimeError("down")
            self.assertEqual(X.run_one(0, sid2, self.a, X.Meter(), runner)["status"], want)
        return

        def boom(model, system, prompt):
            raise RuntimeError("down")
        self.assertEqual(X.run_one(0, SID, self.a, X.Meter(), boom)["status"], "call_error")

    def test_real_session_id_pattern_in_content_is_rejected_without_a_call(self):
        calls = []
        sess = {"session_id": SID, "date": DATE, "turns": [{"role": "user", "content": "see answer_c6fd8ebd"}, {"role": "assistant", "content": "ok"}]}
        json.dump(sess, open(os.path.join(self.d, "sessions", SID + ".json"), "w"))
        rec = X.run_one(0, SID, self.a, X.Meter(), runner_for(GOOD, calls))
        self.assertEqual((rec["status"], calls), ("rejected", []))
        self.assertTrue(rec["reject_reason"])

    def test_cap_stop_file_token_stop_and_no_key_read(self):
        self.assertEqual((X.CAP, X.TOKEN_STOP), (200, 12000000))
        m = X.Meter()
        m.add(12_000_001)
        self.assertTrue(m.over())
        src = open(X.__file__).read()
        self.assertNotIn("stage_x0_key/", src.replace("never read", "").replace('stage_x0_key is never read', ""))


class NotRun(unittest.TestCase):
    def test_placeholders_only_for_missing_sessions(self):
        d = tempfile.mkdtemp()
        old = X.X
        X.X = d
        try:
            os.makedirs(os.path.join(d, "out"))
            json.dump({"status": "ok"}, open(os.path.join(d, "out", "sA.json"), "w"))
            made = X.write_not_run(["sA", "sB", "sC"], 0, 3)
            self.assertEqual(made, ["sB", "sC"])
            r = json.load(open(os.path.join(d, "out", "sB.json")))
            self.assertEqual((r["status"], r["order_index"], r["facts"]), ("not_run", 1, []))
            self.assertEqual(json.load(open(os.path.join(d, "out", "sA.json")))["status"], "ok")
        finally:
            X.X = old


class Cap30(unittest.TestCase):
    def test_variant_differs_only_in_the_fact_cap_number(self):
        from phasef.prompts import EXTRACT_V2_CAP30_TEMPLATE as C, EXTRACT_V2_TEMPLATE as B
        self.assertEqual(B.count("At most 12 facts per session"), 1)
        self.assertEqual(C, B.replace("At most 12 facts per session", "At most 30 facts per session"))
        import difflib
        diff = [l for l in difflib.ndiff(B.splitlines(), C.splitlines()) if l[:2] in ("- ", "+ ")]
        self.assertEqual(len(diff), 2)
        a, b = diff[0][2:], diff[1][2:]
        self.assertEqual(a.replace("12", "30"), b)
        self.assertEqual(X.SHA16, "a2336c7976bb8fc2")
        self.assertNotEqual(X.SHA16_CAP30, X.SHA16)


class Pref(unittest.TestCase):
    def test_pref_template_differs_from_cap30_only_by_the_added_sentence(self):
        from phasef.prompts import EXTRACT_V2_CAP30_TEMPLATE as C, EXTRACT_V3_PREF_TEMPLATE as P, PREF_SENTENCE
        self.assertEqual(P.count(PREF_SENTENCE), 1)
        self.assertEqual(P.replace(PREF_SENTENCE + " ", ""), C)
        self.assertIn("Skip small talk. " + PREF_SENTENCE + " At most 30 facts per session", P)
        self.assertEqual(X.SHA16_CAP30, "ae055bca99452fb6")
        self.assertNotIn(X.SHA16_PREF, (X.SHA16, X.SHA16_CAP30))

    def test_replicate_gives_a_distinct_cache_namespace_with_identical_prompt_fields(self):
        d = tempfile.mkdtemp()
        old = (X.X, X.REPLICATE)
        X.X = d
        os.makedirs(os.path.join(d, "sessions"))
        json.dump({"session_id": SID, "date": DATE, "turns": TURNS}, open(os.path.join(d, "sessions", SID + ".json"), "w"))
        calls = []
        try:
            X.set_variant("cap30")
            X.REPLICATE = None
            r0 = X.run_one(0, SID, None, X.Meter(), runner_for(GOOD, calls))
            r1 = X.run_one(0, SID, None, X.Meter(), runner_for(GOOD, calls))  # same namespace: served from the cache
            self.assertEqual(len(calls), 1)
            X.REPLICATE = "rep1"
            r2 = X.run_one(0, SID, None, X.Meter(), runner_for(GOOD, calls))
            self.assertEqual(len(calls), 2)  # new namespace: a fresh call
            self.assertEqual((r2["prompt_version"], r2["prompt_sha16"]), (r0["prompt_version"], r0["prompt_sha16"]))
            self.assertEqual((r0["replicate"], r2["replicate"]), (None, "rep1"))
        finally:
            X.X, X.REPLICATE = old
            X.set_variant("base")


class Jobs(unittest.TestCase):
    def test_jobs_run_in_given_order_with_arm_prompts_replicate_and_separate_dirs(self):
        d = tempfile.mkdtemp()
        old = X.X
        X.X = d
        try:
            os.makedirs(os.path.join(d, "sessions"))
            ids = ["sAAAAAAAAAAAA", "sBBBBBBBBBBBB"]
            for i, sid in enumerate(ids):
                json.dump({"session_id": sid, "date": DATE, "turns": TURNS + [{"role": "user", "content": sid}, {"role": "assistant", "content": "k"}]}, open(os.path.join(d, "sessions", sid + ".json"), "w"))
            jobs = [{"order_index": 0, "session_id": ids[0], "arm": "S", "replicate": "x0c-s-1"}, {"order_index": 1, "session_id": ids[0], "arm": "P", "replicate": None},
                    {"order_index": 2, "session_id": ids[1], "arm": "P", "replicate": None}, {"order_index": 3, "session_id": ids[1], "arm": "S", "replicate": "x0c-s-1"}]
            json.dump({"jobs": jobs}, open(os.path.join(d, "run_order.json"), "w"))
            a = Args()
            a.concurrency, a.remote, a.stop_file = 1, "", os.path.join(d, "STOP")
            calls = []

            def factory():
                def run(model, system, prompt):
                    calls.append(prompt)
                    sid = ids[0] if ids[0] in prompt else ids[1]
                    return {"result": json.dumps({"sessions": [{"session_id": sid, "facts": [{"subject": "u", "slot": "u.x", "predicate": "p", "object": "o", "round": 0, "author": "user", "quote": "q"}]}]}),
                            "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}, "total_cost_usd": 0.1}
                return run
            stats = X.run_jobs(a, factory())
            self.assertEqual(len(calls), 4)
            self.assertIn("At most 30 facts", calls[0])
            self.assertNotIn("preferences, interests, wishes and plans", calls[0])  # arm S
            self.assertIn("preferences, interests, wishes and plans", calls[1])  # arm P
            self.assertEqual([("S" if "preferences, interests" not in c else "P") for c in calls], ["S", "P", "P", "S"])
            rs = json.load(open(os.path.join(d, "out", "S", ids[0] + ".json")))
            rp = json.load(open(os.path.join(d, "out", "P", ids[0] + ".json")))
            self.assertEqual((rs["arm"], rs["replicate"], rs["prompt_version"], rs["prompt_sha16"]), ("S", "x0c-s-1", "extract-v2-cap30", "ae055bca99452fb6"))
            self.assertEqual((rp["arm"], rp["replicate"], rp["prompt_version"], rp["prompt_sha16"]), ("P", None, "extract-v3-pref", "56c79c2119472532"))
            self.assertTrue(os.path.exists(os.path.join(d, "out", rs["raw_output_path"])))
            self.assertEqual(sum(stats.values()), 4)
        finally:
            X.X = old


if __name__ == "__main__":
    unittest.main()
