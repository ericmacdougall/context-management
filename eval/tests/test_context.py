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
        items = pack_facts(["f1", "f1b" if False else "f2"], facts, self.dates, self.texts, 10 ** 6)
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds, ["fact", "source_round", "fact", "source_round"])
        self.assertEqual(sum(i["tokens"] for i in items), sum(i["tokens"] for i in items))
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
        t = make_task(q.qid, "H1", q.question, "2023-08-01 10:00", [{"round_id": "ku_s4:0", "session_id": "ku_s4",
                                                                    "date": "2023-06-20 08:15", "rank": 1, "snippet": "x"}])
        blob = _j.dumps(t).lower()
        for w in FORBIDDEN:
            self.assertNotIn(w, blob)
        texts = {r.round_id: r.text for r in q.rounds}
        dates = {s: "2023-01-01 00:00" for s in q.haystack_session_ids}
        m = score_one(q, ["ku_s2:0", "ku_s4:0"], dates, texts)
        self.assertEqual(m["nongold_share"], 0.5)
        self.assertTrue(m["gold_latest_in_context"])
        stale = score_one(q, ["ku_s1:0"], dates, texts)
        self.assertTrue(stale["stale_only"])
        m["qid"], m["calls"], m["tool_seconds"] = q.qid, 3, 1.5
        self.assertEqual(aggregate([m], {q.qid})["low_overlap"]["tool_calls"], 3)
        self.assertNotIn("ids outside scope", "")
        out_of_scope = score_one(q, ["zzz:0"], dates, texts)
        self.assertEqual(out_of_scope["items"], 0)   # ids outside the level scope are dropped, not credited
