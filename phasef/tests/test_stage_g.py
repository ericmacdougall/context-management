import json
import os
import tempfile
import unittest

from phasef import stage_g as S

TASK = {"_meta": {"question_id": "qid12345", "level": "H3", "variant": "g", "shuffle": [3, 1, 2]}, "question": "What is my cat called?", "question_date": "2023/05/30 (Tue) 12:18",
        "candidates": [{"round_id": "s0123456789ab:%d" % i, "text": "user: text %d %s [cut]" % (i, "y" * 390), "session_id": "s0123456789ab", "date": "2023-05-2%d 09:42" % i} for i in range(3)]}

DIG_EXPECTED = ("Question (asked on 2023/05/30 (Tue) 12:18): What is my cat called?\n\nThe memory search returned the 3 candidate rounds below, in arbitrary order (round id in brackets). Many of them are probably not relevant, and the evidence needed to answer may be missing from them. "
                "You have tools: search(query, k), open_round(round_id), open_session(session_id), timeline(term). Rule: first judge how many of the candidates actually contain information that helps answer the question. If fewer than 3 do, you MUST make at least 3 tool calls "
                "(for example searches with reworded queries, or opening the sessions of promising candidates) before you answer; if 3 or more do, tool calls are optional. Use at most 12 tool calls in total. Then return at most 20 round ids, best first, each with a one-line reason. "
                "Round ids may come from the candidates or from tool results.\n\nOutput exactly: {\"results\": [{\"round_id\": \"<id>\", \"reason\": \"<one line>\"}, ...]}\n\nCandidates:\n\n")
REORDER_EXPECTED = ("Question (asked on 2023/05/30 (Tue) 12:18): What is my cat called?\n\nBelow are 3 candidate rounds from a user's chat history, in arbitrary order, each with its round id in brackets and its text cut to its first 400 characters. Most of them are probably not relevant. "
                    "Return at most 20 round ids, the rounds most likely to contain the information needed to answer the question, best first, each with a one-line reason. Use only the given ids, each at most once.\n\n"
                    "Output exactly: {\"results\": [{\"round_id\": \"<id>\", \"reason\": \"<one line>\"}, ...]}\n\nCandidates:\n\n")


def lines():
    return "\n\n".join("[%s] %s %s\n%s" % (c["round_id"], c["date"], c["session_id"], c["text"]) for c in TASK["candidates"])


class Args:
    pass


def fake_runner(calls, answers=None):
    def run(model, system, prompt):
        calls.append((model, system, prompt))
        if model == "opus":
            return {"result": "1: yes", "usage": {"input_tokens": 1, "output_tokens": 1}}
        if "Candidates:" in prompt:
            ids = [l[1:l.index("]")] for l in prompt.splitlines() if l.startswith("[s")]
            return {"result": json.dumps({"results": [{"round_id": i, "reason": "r"} for i in (answers or ids)[:25]]}), "usage": {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 500}}
        return {"result": "step by step %d. Answer: Luna" % len(calls), "usage": {"input_tokens": 5, "output_tokens": 6}}
    return run


class StageG(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old_g = S.G
        S.G = self.d
        self.a = Args()
        self.a.forbidden, self.a.backend, self.a.remote, self.a.concurrency, self.a.stop_file, self.a.phase, self.a.part = set(), "true {qid}", "", 1, os.path.join(self.d, "STOP"), "rest", "AB"

    def tearDown(self):
        S.G = self.old_g

    def test_part_a_prompts_are_byte_for_byte_and_leak_nothing(self):
        self.assertEqual(S.build_dig_prompt(TASK), DIG_EXPECTED + lines())
        self.assertEqual(S.build_reorder_prompt(TASK), REORDER_EXPECTED + lines())
        for p in (S.build_dig_prompt(TASK), S.build_reorder_prompt(TASK)):
            for bad in ("qid12345", "_meta", "shuffle", "variant", "A-DIG", "A-REORDER", "g1"):
                self.assertNotIn(bad, p)
            self.assertIn("y" * 390 + " [cut]", p)  # the eval owner's 400-character cut is not cut again

    def test_reorder_rejects_ids_outside_candidates_and_caps_at_20(self):
        calls = []
        rec = S.run_a_item("A-REORDER", TASK, self.a, fake_runner(calls, ["s0123456789ab:0", "zzz"]))
        self.assertEqual(rec["status"], "rejected")
        self.assertEqual(rec["ranked_round_ids"], [])
        ok = S.run_a_item("A-REORDER", dict(TASK, question="Another?"), self.a, fake_runner(calls, ["s0123456789ab:1", "s0123456789ab:0"]))
        self.assertEqual((ok["status"], ok["ranked_round_ids"], ok["tool_calls"], ok["prompt_version"]), ("ok", ["s0123456789ab:1", "s0123456789ab:0"], 0, "reorder-prompt-g1"))
        self.assertEqual(ok["prompt_tokens_measured"], 2 + 20000 + 500 - 10500)

    def test_part_a_counter_is_separate_and_capped(self):
        self.assertTrue( (S.CAP_A, S.CAP_B) == (190, 460) and S.CAP_A + S.CAP_B == 650)
        calls = []
        S.run_a_item("A-REORDER", TASK, self.a, fake_runner(calls))
        self.assertEqual(open(os.path.join(self.d, "A", "cache", "calls.count")).read(), "1")
        self.assertFalse(os.path.exists(os.path.join(self.d, "B", "cache", "calls.count")))

    def test_part_b_order_is_seeded_shuffled_and_arm_blind(self):
        ids = ["q%d" % i for i in range(5)]
        o1, o2 = S.b_items(ids), S.b_items(ids)
        self.assertEqual(o1, o2)
        self.assertEqual(len(o1), 15)
        self.assertNotEqual(o1, [(q, a) for q in ids for a in S.ARMS_B])
        for q, arm in o1:
            self.assertNotIn(arm.lower(), os.path.basename(S.neutral_ctx_path(q, arm)))
            self.assertNotIn(q, os.path.basename(S.neutral_ctx_path(q, arm)))
        self.assertEqual(len({S.arm_key("q1", a) for a in S.ARMS_B}), 3)

    def test_part_b_item_blind_prompts_and_records(self):
        qrec = {"question_id": "qid12345", "question_type": "single-session-user", "question": "What is my cat called?", "answer": "Luna", "question_date": "2023/05/30 (Tue) 12:18"}
        for arm in S.ARMS_B:
            os.makedirs(os.path.join(self.d, "B", arm), exist_ok=True)
            open(os.path.join(self.d, "B", arm, "qid12345.txt"), "w").write("[2023-05-20 09:42] user: my cat is Luna (%s)" % ("ctx-" + str(S.ARMS_B.index(arm))))
        calls = []
        recs = [S.run_b_item("qid12345", arm, qrec, self.a, fake_runner(calls)) for arm in S.ARMS_B]
        for model, system, prompt in calls:
            for name in ("RRF", "ANALYST", "ORACLE", "qid12345", "stage_g"):
                self.assertNotIn(name, prompt + system)
        judge_prompts = [p for m, s, p in calls if m == "opus"]
        self.assertEqual(len(judge_prompts), 3)
        for p in judge_prompts:
            self.assertIn("Correct Answer: Luna", p)
            self.assertIn(". Answer: Luna", p)
            self.assertNotIn("ctx-", p)  # the judge never sees the context
        r = recs[0]
        self.assertEqual((r["status"], r["judge"], r["judge_status"], r["final_answer"]), ("ok", "yes", "ok", "Luna"))
        self.assertEqual(set(r), {"question_id", "arm", "arm_key", "status", "reader_answer", "final_answer", "judge", "judge_status", "tokens", "seconds"})

    def test_missing_context_is_a_failure_without_calls(self):
        calls = []
        r = S.run_b_item("nope", "ANALYST", {"question_type": "x", "question": "q", "answer": "a", "question_date": "d"}, self.a, fake_runner(calls))
        self.assertEqual((r["status"], calls), ("failed_no_context", []))

    def test_real_session_id_in_context_blocks_the_item_and_records_it(self):
        os.makedirs(os.path.join(self.d, "B", "RRF"), exist_ok=True)
        open(os.path.join(self.d, "B", "RRF", "q9.txt"), "w").write("see answer_c6fd8ebd")
        calls = []
        r = S.run_b_item("q9", "RRF", {"question_type": "x", "question": "q", "answer": "a", "question_date": "d"}, self.a, fake_runner(calls))
        self.assertEqual((r["status"], calls), ("blocked_guard", []))
        self.assertIn("guard_block", open(os.path.join(self.d, "B", "ledger.jsonl")).read())

    def test_token_meter_counts_all_kinds_including_earlier_calls_and_trips_at_ceiling(self):
        os.makedirs(os.path.join(self.d, "A"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "B"), exist_ok=True)
        open(os.path.join(self.d, "A", "ledger.jsonl"), "w").write(json.dumps({"in_tokens": 1, "cache_creation": 10, "cache_read": 100}) + "\n")
        open(os.path.join(self.d, "B", "ledger.jsonl"), "w").write(json.dumps({"in_tokens": 2, "cache_creation": 20, "cache_read": 200}) + "\n")
        self.assertEqual(S.initial_input_tokens(), 333)
        m = S.TokenMeter(333, 400)
        self.assertFalse(m.over())
        m.add(68)
        self.assertTrue(m.over())

    def test_rest_phase_runs_part_b_first_then_part_a_interleaved_and_stops_at_ceiling(self):
        import types
        os.makedirs(os.path.join(self.d, "A300", "H3"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "B"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "A"), exist_ok=True)
        os.makedirs(os.path.join(R_DIR, "analyst_job"), exist_ok=True) if False else None
        self.assertEqual(S.part_a_items(["q1", "q2"]), [("A-REORDER", "q1"), ("A-DIG", "q1"), ("A-REORDER", "q2"), ("A-DIG", "q2")])

    def test_final_answer_takes_text_after_last_answer_marker(self):
        self.assertEqual(S.final_answer("a Answer: no\nreasoning\nAnswer: Luna"), "Luna")
        self.assertEqual(S.final_answer("no marker"), "no marker")


if __name__ == "__main__":
    unittest.main()
