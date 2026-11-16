import unittest

from emlab.context import (context_metrics, est_tokens, order_items, pack_facts, pack_mix, pack_rag, render_round)
from emlab.dataset import FixtureLoader
from emlab.facts import FactsRetriever


class Ctx(unittest.TestCase):
    def setUp(self):
        self.q = list(FixtureLoader().questions())[0]
        rs = self.q.rounds
        self.texts = {r.round_id: r.text for r in rs}
        self.dates = {t.session_id: t.date.strftime("%Y-%m-%d") for t in self.q.turns}

    def test_est_tokens(self):
        self.assertEqual(est_tokens("abcd"), 1)
        self.assertEqual(est_tokens("abcde"), 2)

    def test_rag_strict_prefix_budget(self):
        ranked = ["ku_s4:0", "ku_s1:0", "ku_s2:0"]
        first = est_tokens(render_round(self.dates["ku_s4"], self.texts["ku_s4:0"]))
        self.assertEqual([i["item_id"] for i in pack_rag(ranked, self.dates, self.texts, first)], ["ku_s4:0"])
        self.assertEqual(pack_rag(ranked, self.dates, self.texts, first - 1), [])
        big = pack_rag(ranked, self.dates, self.texts, 10 ** 6)
        self.assertEqual([i["item_id"] for i in big], ranked)

    def _facts(self):
        return {"f1": {"id": "f1", "session_id": "ku_s4", "round_index": 0, "text": "works at Borealis Labs", "slot": "employer",
                       "value": "Borealis Labs", "cue_keys": ["job", "employer"]},
                "f2": {"id": "f2", "session_id": "ku_s1", "round_index": 0, "text": "works at Acme Corp", "slot": "employer",
                       "value": "Acme Corp", "cue_keys": ["job"]}}

    def test_facts_include_source_round_once_and_respect_budget(self):
        facts = self._facts()
        items = pack_facts(["f1", "f2"], facts, self.dates, self.texts, 10 ** 6)
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds, ["fact", "source_round", "fact", "source_round"])
        # a second fact from an already-included round adds its own tokens but not a second copy of the round
        facts["f3"] = dict(facts["f1"], id="f3", text="also at Borealis")
        again = pack_facts(["f1", "f3"], facts, self.dates, self.texts, 10 ** 6)
        self.assertEqual([i["kind"] for i in again], ["fact", "source_round", "fact"])
        tight = pack_facts(["f1", "f2"], facts, self.dates, self.texts, items[0]["tokens"] + items[1]["tokens"])
        self.assertEqual(len(tight), 2)  # f2 does not fit: strict prefix stops

    def test_mix_dedupes_by_source_round_and_splits_budget(self):
        facts = self._facts()
        mix = pack_mix(["f1"], ["ku_s4:0", "ku_s1:0"], facts, self.dates, self.texts, 10 ** 6)
        ids = [i["round_id"] for i in mix if i["kind"] in ("round", "source_round")]
        self.assertEqual(len(ids), len(set(ids)))        # ku_s4:0 not repeated by the RAG half
        self.assertIn("ku_s1:0", ids)

    def test_order_is_chronological_and_metrics_stale_only(self):
        items = pack_rag(["ku_s4:0", "ku_s1:0"], self.dates, self.texts, 10 ** 6)
        self.assertEqual([i["session_id"] for i in order_items(items)], ["ku_s1", "ku_s4"])
        stale = [i for i in items if i["session_id"] == "ku_s1"]
        m = context_metrics(self.q, stale)
        self.assertFalse(m["gold_latest_in_context"])
        self.assertTrue(m["stale_only"])                 # stale-only counts as a miss
        self.assertTrue(context_metrics(self.q, items)["gold_latest_in_context"])
        self.assertFalse(context_metrics(self.q, items)["stale_only"])

    def test_cue_leg_can_be_switched_off(self):
        fr = FactsRetriever(list(self._facts().values()))
        with_cue = fr.rank("zzz", ["employer"], None, ["ku_s1", "ku_s4"])
        without = fr.rank("zzz", ["employer"], None, ["ku_s1", "ku_s4"], use_cue=False)
        self.assertEqual(with_cue, ["f1"])      # found only through the cue key
        self.assertEqual(without, [])

    def test_facts_retriever_cue_and_text(self):
        fr = FactsRetriever(list(self._facts().values()))
        r = fr.rank("Which employer and job?", ["employer"], None, ["ku_s1", "ku_s4"])
        self.assertEqual(set(r), {"f1", "f2"})
        self.assertEqual(r[0], "f1")            # f1 matches both cue keys
        self.assertEqual(fr.rank("Which employer?", ["employer"], None, ["ku_s1"]), [])  # no match, only searched sessions
        self.assertEqual(fr.rank("x", [], None, ["nope"]), [])


if __name__ == "__main__":
    unittest.main()


class Dates(unittest.TestCase):
    def test_synthetic_date_rules(self):
        from datetime import datetime
        from emlab.build_contexts import event_time_allowed, synthetic_date
        e, q = datetime(2023, 1, 1, 8, 0), datetime(2023, 6, 1, 10, 0)
        a = synthetic_date("q1", "s1", e, q)
        self.assertEqual(a, synthetic_date("q1", "s1", e, q))                 # deterministic
        self.assertNotEqual(a, synthetic_date("q1", "s2", e, q))
        for i in range(200):
            d = synthetic_date("q%d" % i, "s%d" % i, e, q)
            self.assertTrue(e <= d <= q)                                       # never after the question date
        f = {"event_time": "2023-03-01", "ref_date": "2023-02-10 09:00"}
        self.assertTrue(event_time_allowed(f, "2023-02-10 09:00", False))
        self.assertFalse(event_time_allowed(f, "2023-02-11 09:00", False))
        self.assertFalse(event_time_allowed(f, "2023-02-10 09:00", True))      # synthetic date: never shown
        self.assertIsNone(event_time_allowed({"event_time": "x"}, "2023-02-10 09:00", False))


class Analyst(unittest.TestCase):
    def test_task_has_no_gold_and_score_precision(self):
        from emlab.analyst_prep import FORBIDDEN, make_task
        from emlab.analyst_score import aggregate, score_one
        import json as _j
        q = list(FixtureLoader().questions())[0]
        t = make_task(q.qid, "H1", q.question, "2023-08-01 10:00", [{"round_id": "s123456789abc:0", "session_id": "s123456789abc",
                                                                    "date": "2023-06-20 08:15", "text": "x"}])
        mf = {k: v for k, v in t.items() if k != "_meta"}            # what a model may see
        self.assertEqual(set(t), {"_meta", "question", "question_date", "candidates"})
        self.assertEqual(t["_meta"]["question_id"], q.qid)
        blob = _j.dumps(mf).lower()
        for w in FORBIDDEN:
            self.assertNotIn(w, blob)
        from emlab.guard import check as _check
        _check(t, {"ku_s4"}, {q.qid})                                 # _meta is skipped; model-facing part is clean
        texts = {r.round_id: r.text for r in q.rounds}
        dates = {s: "2023-01-01 00:00" for s in q.haystack_session_ids}
        m = score_one(q, ["ku_s2:0", "ku_s4:0"], dates, texts)
        self.assertEqual(m["nongold_share"], 0.5)
        self.assertTrue(m["gold_latest_in_context"])
        stale = score_one(q, ["ku_s1:0"], dates, texts)
        self.assertTrue(stale["stale_only"])
        m["qid"], m["calls"], m["tool_seconds"] = q.qid, 3, 1.5
        self.assertEqual(aggregate([m], {q.qid})["low_overlap"]["tool_calls"], 3)
        out_of_scope = score_one(q, ["zzz:0"], dates, texts)
        self.assertEqual(out_of_scope["items"], 0)   # ids outside the level scope are dropped, not credited


class Anon(unittest.TestCase):
    def test_opaque_ids_hide_the_label_and_roundtrip(self):
        from emlab.anon import AnonMap, opaque_rid, opaque_sid
        sids = ["answer_abc_1", "sharegpt_x", "0123abcd", "ultrachat_9"]
        for s in sids:
            self.assertNotIn("answer", opaque_sid(s))
            self.assertNotIn("sharegpt", opaque_sid(s))
        am = AnonMap(sids)
        self.assertEqual(am.real_rid(opaque_rid("answer_abc_1:3")), "answer_abc_1:3")
        self.assertEqual(opaque_rid("sharegpt_x:0"), opaque_sid("sharegpt_x") + ":0")

    def test_result_ids_outside_candidates_and_tool_returns_are_rejected(self):
        import json as _j, tempfile, os as _o
        from emlab.anon import AnonMap, opaque_rid
        from emlab.analyst_score import allowed_ids, real_ranked
        am = AnonMap(["s_a", "s_b", "s_c"])
        task = {"candidates": [{"round_id": opaque_rid("s_a:0")}]}
        d = tempfile.mkdtemp()
        log = _o.path.join(d, "log.jsonl")
        open(log, "w").write(_j.dumps({"qid": "q", "level": "H1", "returned_ids": [opaque_rid("s_b:1")]}) + "\n"
                             + _j.dumps({"qid": "other", "level": "H1", "returned_ids": [opaque_rid("s_c:2")]}) + "\n")
        ok = allowed_ids(task, log, "q", "H1")
        good, rejected = real_ranked({"ranked_round_ids": [opaque_rid("s_a:0"), opaque_rid("s_b:1"), opaque_rid("s_c:2")]}, ok, am)
        self.assertEqual(good, ["s_a:0", "s_b:1"])
        self.assertEqual(rejected, 1)     # s_c:2 was returned for another question only


class Guard(unittest.TestCase):
    def test_guard_refuses_real_ids_question_ids_levels_and_label_patterns(self):
        from emlab.guard import LeakError, check
        real = {"answer_ab12cd_1", "0123abcd", "sharegpt_X1"}
        qids = {"6e984302", "gpt4_77_abs"}
        check({"round_id": "s123456789abc:3", "text": "hello world, H1B visa"}, real, qids)   # opaque ids pass; content may say H1
        check({"_meta": {"question_id": "6e984302", "level": "H3"}, "question": "q"}, real, qids)  # _meta is skipped
        for bad in ({"session_id": "answer_ab12cd_1"}, {"round_id": "0123abcd:4"}, {"x": ["see answer_ff00aa_2"]},
                    {"text": "from sharegpt_Q9 conversation"}, {"ultrachat_77": 1},
                    {"item_id": "src:0123abcd:3"}, {"fact_id": "0123abcd:2:0"}, {"note": "see 0123abcd now"},
                    {"question_id": "6e984302"}, {"level": "H3"}, {"label": "gpt4_77_abs"}, {"text": "uses 0123abcd inside"}):
            with self.assertRaises(LeakError):
                check(bad, real, qids)

    def test_write_context_refuses_leaky_items(self):
        import tempfile
        from emlab.context import write_context
        from emlab.guard import LeakError
        q = list(FixtureLoader().questions())[0]
        item = {"item_id": "x", "kind": "round", "session_id": "ku_s4", "round_id": "ku_s4:0", "round_index": 0, "date": "d",
                "text": "t", "tokens": 1, "rank": 0}
        with self.assertRaises(LeakError):
            write_context(tempfile.mkdtemp(), "rag", "H0", q, 10, [item], {"ku_s4"})
        ok = dict(item, session_id="s123456789abc", round_id="s123456789abc:0", item_id="s123456789abc:0")
        path = write_context(tempfile.mkdtemp(), "rag", "H1", q, 10, [ok], {"ku_s4"}, {q.qid})
        import json as _j
        doc = _j.load(open(path))
        self.assertEqual(set(doc), {"_meta", "items"})                # question id and level live only under _meta
        self.assertEqual(doc["_meta"]["level"], "H1")


class Tools(unittest.TestCase):
    def test_timeline_reveals_no_pool_size(self):
        from emlab.analyst_tools import Backend
        b = object.__new__(Backend)
        n = 80
        b.sessions = {"sess%d" % i: "2023-01-%02d 00:00" % (i % 28 + 1) for i in range(n)}
        b.chunks = [{"chunk_id": "sess%d:0#0" % i, "round_id": "sess%d:0" % i, "session_id": "sess%d" % i} for i in range(n)]
        b.by_session = {"sess%d" % i: [i] for i in range(n)}
        b.round_text = {"sess%d:0" % i: "user: topic %d" % i for i in range(n)}
        r = b.timeline(None, 5)
        self.assertEqual(set(r), {"sessions", "more"})
        self.assertEqual(len(r["sessions"]), 5)
        self.assertTrue(r["more"])
        self.assertFalse(b.timeline(None, 50)["more"] and n <= 50)
        small = object.__new__(Backend)
        small.sessions, small.chunks, small.by_session, small.round_text = (
            {"a": "2023-01-01 00:00"}, [{"chunk_id": "a:0#0", "round_id": "a:0", "session_id": "a"}], {"a": [0]}, {"a:0": "user: x"})
        self.assertFalse(small.timeline(None, 5)["more"])


class Registered(unittest.TestCase):
    def test_one_rerank_head_size(self):
        from emlab.retrievers import RERANK_TOP_N, run_arm, FakeRetriever
        self.assertEqual(RERANK_TOP_N, 20)
        seen = []

        class R(FakeRetriever):
            def rerank(self, q, ranked, depth):
                seen.append(len(ranked))
                return ranked

        q = list(FixtureLoader().questions())[0]
        run_arm(R(), q, "rerank", 200, "off")
        self.assertLessEqual(seen[0], RERANK_TOP_N)
        import inspect
        self.assertEqual(inspect.signature(run_arm).parameters["rerank_top"].default, 20)

    def test_pool_lowoverlap_generator_excludes_abstention_and_has_thresholds(self):
        from emlab.lowoverlap_pool import compute
        from emlab.retrievers import FakeRetriever
        from emlab.pool import PoolContext
        qs = list(FixtureLoader().questions())
        res = compute(qs, PoolContext(qs), FakeRetriever())
        self.assertNotIn("ab_002_abs", res["ranks"])
        self.assertEqual(set(res["thresholds"]), {"100", "50", "20"})
        self.assertEqual(res["n_with_gold"], 2)


class Budget(unittest.TestCase):
    def test_budget_is_keyed_on_variant_and_ignores_probe_rows(self):
        import json as _j, os as _o, tempfile
        from emlab.analyst_tools import budget_state
        p = _o.path.join(tempfile.mkdtemp(), "log.jsonl")
        rows = [{"qid": "q", "level": "H3", "variant": "", "seconds": 1.0}] * 10 + \
               [{"qid": "q", "level": "H3", "variant": "top20", "seconds": 2.0}] * 3 + \
               [{"qid": "other", "level": "H3", "variant": "", "seconds": 5.0}]
        open(p, "w").write("\n".join(_j.dumps(r) for r in rows) + "\n")
        self.assertEqual(budget_state(p, "q", "H3"), {"calls": 10, "seconds": 10.0})            # the top-20 run does not eat it
        self.assertEqual(budget_state(p, "q", "H3", "top20"), {"calls": 3, "seconds": 6.0})
        self.assertEqual(budget_state(p, "nobody", "H3"), {"calls": 0, "seconds": 0.0})


class StageG(unittest.TestCase):
    def test_string_match_heuristic(self):
        from emlab.stage_g_score import final_line, string_match
        self.assertTrue(string_match("Borealis Labs", "I now work at borealis labs."))
        self.assertTrue(string_match("25 minutes and 50 seconds (or 25:50)", "Answer: 25 minutes, 50 seconds"))   # numbers 25 and 50
        self.assertTrue(string_match("1,200 dollars", "about 1200 dollars"))
        self.assertFalse(string_match("four", "three"))
        self.assertFalse(string_match("Paris", ""))
        self.assertEqual(final_line("steps\nAnswer: the suburbs"), "the suburbs")

    def test_digging_compliance_is_mechanical(self):
        from emlab.stage_g_score import DIG_N, followed
        self.assertEqual(DIG_N, 3)
        self.assertTrue(followed(3))
        self.assertFalse(followed(2))
        self.assertFalse(followed(None))

    def test_audit_sample_is_stratified_seeded_and_stable(self):
        from emlab.stage_g_score import audit_sample
        rows = [{"question_id": "q%d" % i, "arm": "A", "judge_yes": i % 3 != 0} for i in range(90)]
        a = audit_sample(rows)
        self.assertEqual(sum(1 for r in a if r["judge_yes"]), 20)
        self.assertEqual(sum(1 for r in a if not r["judge_yes"]), 20)
        self.assertEqual(a, audit_sample(list(reversed(rows))))
        small = audit_sample(rows[:6])
        self.assertEqual(len(small), 6)


class StageX0(unittest.TestCase):
    def test_answer_match_heuristic(self):
        from emlab.stage_x0_score import answer_match
        self.assertTrue(answer_match("Borealis Labs", "works at borealis labs now")["string"])
        self.assertTrue(answer_match("25 minutes and 50 seconds", "ran 25 minutes 50 seconds")["key"])
        self.assertFalse(answer_match("25 minutes and 50 seconds", "ran 30 minutes")["key"])
        self.assertFalse(answer_match("Paris", "")["key"])
        self.assertTrue(answer_match("red", "tired")["string"])  # substring heuristic: crude by design, labelled as such

    def test_bm25_ranks_the_matching_doc_first(self):
        from emlab.stage_x0_score import bm25_rank
        docs = [["cat", "sat"], ["dog", "barked", "loudly"], ["hamster", "wheel", "cage"]]
        self.assertEqual(bm25_rank(docs, ["hamster", "cage"])[0], 2)
        self.assertEqual(bm25_rank(docs, ["unseen"]), [0, 1, 2])

    def test_spearman_and_extrapolation(self):
        from emlab.stage_x0_score import extrapolate, spearman
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        e = extrapolate({0: [10.0, 10.0], 1: [30.0, 30.0]}, 1000)
        self.assertAlmostEqual(e["per_call"], 20.0)
        self.assertAlmostEqual(e["total"], 20000.0)
        self.assertLessEqual(e["total_low"], e["total"] + 1e-9)

    def test_vendored_provenance_rejects_changed_quote(self):
        from emlab import provenance_x04 as P
        sess = {"s1": P.rounds_of_turns([{"role": "user", "content": "I adopted a dog named Rex"}, {"role": "assistant", "content": "Nice"}])}
        ok = P.check_all([{"session_id": "s1", "round": 0, "quote": "adopted a dog named Rex", "author": "user"}], sess)[0]
        bad = P.check_all([{"session_id": "s1", "round": 0, "quote": "adopted a cat named Rex", "author": "user"}], sess)[0]
        self.assertEqual((ok["a"], ok["b"]), ("pass", "pass"))
        self.assertEqual(bad["a"], "fail")

    def test_allocate_and_prep_lengths(self):
        from emlab.stage_x0_prep import allocate
        self.assertEqual(sum(allocate([0.2] * 5, 121)), 121)
        self.assertEqual(allocate([0.2] * 5, 121), [25, 24, 24, 24, 24])
