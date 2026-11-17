import json
import os
import stat
import tempfile
import unittest
import urllib.request

from datetime import date, datetime

from emlab.corpus import cluster_ids, unique_rounds
from emlab.dataset import FixtureLoader, as_of_end_of_day, split_pilot_test, visible_as_of
from emlab.retrievers import allowed_sessions, apply_boost
from emlab.timeparse import parse_question_range
from emlab.es_client import EsClient, EsError
from emlab.mappings import bm25_query, build_mapping, knn_query
from emlab.retrievers import FakeRetriever, run_arm, to_sessions
from emlab import runner


class Fixture(unittest.TestCase):
    def setUp(self):
        self.qs = list(FixtureLoader().questions())

    def test_shape(self):
        self.assertEqual(len(self.qs), 3)
        self.assertEqual(sum(len(q.session_ids) for q in self.qs), 13)
        ku = [q for q in self.qs if q.qtype == "knowledge-update"][0]
        self.assertEqual(ku.gold_session_ids, ["ku_s1", "ku_s4"])
        self.assertEqual(ku.gold_round_ids, ["ku_s1:0", "ku_s4:0"])
        self.assertEqual(ku.gold_latest_session_ids, ["ku_s4"])  # latest-dated gold session is current

    def test_repeated_session_id_in_a_haystack_is_deduplicated(self):
        import json as _json
        from emlab.dataset import LongMemEvalLoader
        rec = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "tiny.json")))[1]
        rec["haystack_session_ids"].append(rec["haystack_session_ids"][0])
        rec["haystack_dates"].append(rec["haystack_dates"][0])
        rec["haystack_sessions"].append(rec["haystack_sessions"][0])
        q = LongMemEvalLoader.parse_record(rec)
        self.assertEqual(len(q.haystack_session_ids), 3)
        self.assertEqual(len(q.rounds), len({r.round_id for r in q.rounds}))

    def test_rounds_pair_user_and_assistant(self):
        ku = self.qs[0]
        r = [x for x in ku.rounds if x.session_id == "ku_s4"]
        self.assertEqual(len(r), 1)
        self.assertIn("user:", r[0].text)
        self.assertIn("assistant:", r[0].text)

    def test_abstention_excluded_from_retrieval_metrics(self):
        ab = [q for q in self.qs if q.is_abstention][0]
        self.assertEqual(ab.retrieval_gold_session_ids, [])
        # even if a gold list were present, abstention must not enter recall/NDCG
        ab2 = type(ab)(**{**ab.__dict__, "gold_session_ids": ["ab_s1"]})
        self.assertEqual(ab2.retrieval_gold_session_ids, [])
        m = runner.question_metrics(ab2, ["ab_s1:0"])
        self.assertTrue(all(v is None for v in m.values()))
        res = runner.evaluate(self.qs, FakeRetriever(), arms=("bm25",), modes=("off",))
        self.assertEqual(res["summary"]["all"]["bm25|off"]["session_recall@5"]["n"], 2)
        self.assertIsNone(runner.low_overlap_own_haystack(ab, FakeRetriever(), 200, 2))

    def test_low_overlap_uses_gold_round_rank_over_full_haystack(self):
        q = self.qs[2]
        ranks = runner.low_overlap_own_haystack_ranks(q, FakeRetriever())
        self.assertIsNotNone(ranks["bm25"])  # full ranking, not truncated at a depth
        self.assertIsNotNone(ranks["dense"])
        self.assertTrue(runner.low_overlap_own_haystack(q, FakeRetriever(), 200, 0))          # every rank > 0
        self.assertFalse(runner.low_overlap_own_haystack(q, FakeRetriever(), 200, 10 ** 6))   # no rank exceeds the bound
        self.assertIsNone(runner.low_overlap_own_haystack_ranks(self.qs[1], FakeRetriever()))  # abstention

    def test_diagnostics_asof(self):
        from emlab.diagnostics import asof_report, parser_report
        r = asof_report(self.qs)
        self.assertEqual(r["gold_sessions_removed_by_day_rule"], 0)
        self.assertEqual(r["gold_sessions_removed_by_strict_timestamp_rule"], 0)  # ku_s6 is not gold here
        self.assertEqual(parser_report(self.qs)["questions"], 3)

    def test_stale_only_retrieval_is_a_gold_latest_miss(self):
        ku = self.qs[0]
        stale_only = runner.question_metrics(ku, ["ku_s1:0", "ku_s2:0"])
        self.assertEqual(stale_only["gold_latest_hit@5"], 0.0)
        self.assertEqual(stale_only["session_recall@5"], 0.5)  # ordinary recall still credits the stale hit
        self.assertEqual(runner.question_metrics(ku, ["ku_s4:0"])["gold_latest_hit@5"], 1.0)
        multi = self.qs[2]
        self.assertIsNone(runner.question_metrics(multi, ["lo_s2:0"])["gold_latest_hit@5"])

    def test_clusters_share_gold_and_split_keeps_them_together(self):
        qs = list(FixtureLoader().questions())
        q = qs[0]
        twin = type(q)(**{**q.__dict__, "qid": "ku_001b"})
        cl = cluster_ids([q, twin, qs[2]])
        self.assertEqual(cl["ku_001"], cl["ku_001b"])
        self.assertNotEqual(cl["ku_001"], cl[qs[2].qid])
        items = [("a%d" % i, "t") for i in range(10)] + [("b", "t"), ("b2", "t")]
        sp = split_pilot_test(items, clusters={"b": "b", "b2": "b"})
        self.assertEqual(sp["b"], sp["b2"])

    def test_day_granularity_asof_keeps_same_day_later_session(self):
        ku = self.qs[0]
        self.assertEqual(ku.late_session_ids, ["ku_s6"])  # dated 17:30, question at 10:00 same day
        allowed, _ = allowed_sessions(ku, "asof")
        self.assertIn("ku_s6", allowed)       # strict timestamps would have dropped it
        d = datetime
        self.assertTrue(visible_as_of(d(2023, 8, 1, 23, 59), d(2023, 8, 1, 0, 1)))
        self.assertFalse(visible_as_of(d(2023, 8, 2, 0, 0), d(2023, 8, 1, 23, 59)))
        self.assertEqual(as_of_end_of_day(d(2023, 8, 1, 10, 0)), "2023-08-01T23:59")

    def test_time_aware_filter_excludes_future_days(self):
        q = self.qs[0]
        q2 = type(q)(**{**q.__dict__, "question_date": datetime(2023, 3, 1)})
        ranked = run_arm(FakeRetriever(), q2, "bm25", 50, "asof")
        self.assertNotIn("ku_s4:0", ranked)
        self.assertIn("ku_s4:0", run_arm(FakeRetriever(), q2, "bm25", 50, "off"))

    def test_expand_filter_and_boost(self):
        q = self.qs[0]
        q2 = type(q)(**{**q.__dict__, "question": "What did I say in June 2023 about work?"})
        allowed, _ = allowed_sessions(q2, "expand_filter")
        self.assertEqual(allowed, {"ku_s4"})
        allowed, boost = allowed_sessions(q2, "expand_boost")
        self.assertIn("ku_s1", allowed)
        self.assertEqual(boost, {"ku_s4"})
        self.assertEqual(apply_boost(["ku_s1:0", "ku_s4:0"], {"ku_s4"}, beta=1.0), ["ku_s4:0", "ku_s1:0"])
        self.assertEqual(apply_boost(["ku_s1:0", "ku_s4:0"], {"ku_s4"}, beta=0.0), ["ku_s1:0", "ku_s4:0"])
        # no parsable range -> fall back to as-of
        q3 = type(q)(**{**q.__dict__, "question": "Where do I work?"})
        self.assertEqual(allowed_sessions(q3, "expand_filter")[0], allowed_sessions(q3, "asof")[0])

    def test_session_ranking_dedupes_by_best_round(self):
        self.assertEqual(to_sessions(["s2:1", "s1:0", "s2:0"]), ["s2", "s1"])

    def test_split_is_deterministic_and_order_independent(self):
        items = [("q%d" % i, "t%d" % (i % 2)) for i in range(20)]
        a = split_pilot_test(items)
        b = split_pilot_test(list(reversed(items)))
        self.assertEqual(a, b)
        self.assertEqual(sum(v == "pilot" for v in a.values()), 4)
        self.assertNotEqual(a, split_pilot_test(items, seed=1))

    def test_dry_run_runs_and_live_needs_paths(self):
        self.assertEqual(runner.main(["--dry-run"]), 0)
        self.assertEqual(runner.main([]), 2)
        self.assertEqual(runner.main(["--seed", "1", "--dataset-path", "x", "--artifacts", "y"]), 2)


class Dev2(unittest.TestCase):
    def _qs(self):
        base = list(FixtureLoader().questions())[0]
        out = []
        for i in range(60):
            out.append(type(base)(**{**base.__dict__, "qid": "k%03d" % i, "qtype": "knowledge-update",
                                     "gold_session_ids": ["g%03d" % i]}))
        out.append(type(base)(**{**base.__dict__, "qid": "k999_abs", "qtype": "knowledge-update",
                                 "gold_session_ids": ["g999"]}))
        out.append(type(base)(**{**base.__dict__, "qid": "m001", "qtype": "multi-session", "gold_session_ids": ["gm"]}))
        return out

    def test_dev2_rule(self):
        qs = self._qs()
        from emlab.dataset import split_assignment, split_pilot_test
        from emlab.corpus import cluster_ids
        sp = split_assignment(qs)
        dev2 = sorted(q for q, v in sp.items() if v == "dev2")
        self.assertEqual(len(dev2), 16)
        base = split_pilot_test([(q.qid, q.qtype) for q in qs], clusters=cluster_ids(qs))
        want = sorted(q for q, v in base.items() if v == "test" and q.startswith("k") and not q.endswith("_abs"))[:16]
        self.assertEqual(dev2, want)                       # first 16 test KU ids in sorted order
        self.assertTrue(all(base[q] == "test" for q in dev2))
        self.assertEqual({q: v for q, v in sp.items() if v == "pilot"}, {q: v for q, v in base.items() if v == "pilot"})
        self.assertNotIn("k999_abs", dev2)
        self.assertEqual(sum(v == "test" for v in sp.values()), sum(v == "test" for v in base.values()) - 16)

    def test_dev2_skips_multi_question_clusters(self):
        qs = self._qs()
        from emlab.dataset import split_assignment, split_pilot_test
        from emlab.corpus import cluster_ids
        base = split_pilot_test([(q.qid, q.qtype) for q in qs], clusters=cluster_ids(qs))
        first = sorted(q for q, v in base.items() if v == "test" and q.startswith("k") and not q.endswith("_abs"))[0]
        i = int(first[1:])
        for q in qs:   # make the first candidate share a gold session with another question: cluster of 2
            if q.qid == "k%03d" % ((i + 1) % 60):
                q.gold_session_ids = ["g%03d" % i]
        sp = split_assignment(qs)
        self.assertNotEqual(sp[first], "dev2")


class TimeParse(unittest.TestCase):
    qd = datetime(2023, 8, 16, 10, 0)  # a Wednesday

    def p(self, t):
        return parse_question_range(t, self.qd)

    def test_absolute(self):
        self.assertEqual(self.p("what happened on 2023/05/20?"), (date(2023, 5, 20), date(2023, 5, 20)))
        self.assertEqual(self.p("on March 3rd"), (date(2023, 3, 3), date(2023, 3, 3)))
        self.assertEqual(self.p("on 9 December"), (date(2022, 12, 9), date(2022, 12, 9)))  # latest past year
        self.assertEqual(self.p("in June 2022"), (date(2022, 6, 1), date(2022, 6, 30)))
        self.assertEqual(self.p("what did I do in May?"), (date(2023, 5, 1), date(2023, 5, 31)))

    def test_relative(self):
        self.assertEqual(self.p("yesterday"), (date(2023, 8, 15), date(2023, 8, 15)))
        self.assertEqual(self.p("last Friday"), (date(2023, 8, 11), date(2023, 8, 11)))
        self.assertEqual(self.p("last week"), (date(2023, 8, 7), date(2023, 8, 13)))
        self.assertEqual(self.p("last month"), (date(2023, 7, 1), date(2023, 7, 31)))
        self.assertEqual(self.p("two weeks ago"), (date(2023, 7, 30), date(2023, 8, 5)))  # Aug 2 +/- 3 days
        self.assertEqual(self.p("in the past 3 days"), (date(2023, 8, 13), date(2023, 8, 16)))

    def test_none(self):
        self.assertIsNone(self.p("Where do I currently work?"))


class CorpusRules(unittest.TestCase):
    def test_unique_rounds_dedupes_and_detects_conflicts(self):
        qs = list(FixtureLoader().questions())
        self.assertEqual(len(unique_rounds(qs + qs)), len(unique_rounds(qs)))
        q = qs[0]
        bad = type(q)(**{**q.__dict__, "turns": [type(t)(**{**t.__dict__, "text": t.text + "!"}) for t in q.turns]})
        with self.assertRaises(ValueError):
            unique_rounds([q, bad])


class EsClientRules(unittest.TestCase):
    def test_non_loopback_refused_by_default(self):
        with self.assertRaises(EsError):
            EsClient("http://es.example.com:9200")
        EsClient("http://127.0.0.1:9200")

    def test_credential_from_file_only_and_never_from_env(self):
        os.environ["ELASTIC_API_KEY"] = "ENVSECRET"
        seen = {}

        class R:
            def read(self):
                return b"{}"

        def opener(req, t):
            seen["auth"] = req.get_header("Authorization")
            return R()

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "key")
            with open(p, "w") as fh:
                fh.write("FILESECRET\n")
            os.chmod(p, 0o600)
            c = EsClient("http://localhost:9200", credential_file=p, opener=opener)
            c.info()
            self.assertEqual(seen["auth"], "ApiKey FILESECRET")
            self.assertNotIn("FILESECRET", repr(c))
            os.chmod(p, 0o644)
            with self.assertRaises(EsError):
                EsClient("http://localhost:9200", credential_file=p)
        c2 = EsClient("http://localhost:9200", opener=opener)
        c2.info()
        self.assertIsNone(seen["auth"])  # env var was not picked up
        del os.environ["ELASTIC_API_KEY"]


class Queries(unittest.TestCase):
    def test_queries_filter_to_haystack_sessions(self):
        f = bm25_query("x", ["s1", "s2"], 10)["query"]["bool"]["filter"]
        self.assertEqual(f, [{"terms": {"session_id": ["s1", "s2"]}}])
        self.assertEqual(knn_query([0.1], ["s1"], 5)["knn"]["filter"], [{"terms": {"session_id": ["s1"]}}])

    def test_mapping_vector_optional(self):
        self.assertNotIn("vector", build_mapping(None)["mappings"]["properties"])
        self.assertEqual(build_mapping(384)["mappings"]["properties"]["vector"]["dims"], 384)


if __name__ == "__main__":
    unittest.main()
