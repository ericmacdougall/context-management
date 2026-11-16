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


if __name__ == "__main__":
    unittest.main()
