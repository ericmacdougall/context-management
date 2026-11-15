import json
import os
import stat
import tempfile
import unittest
import urllib.request

from emlab.dataset import FixtureLoader, split_pilot_test
from emlab.es_client import EsClient, EsError
from emlab.mappings import bm25_query, build_mapping, knn_query
from emlab.retrievers import FakeRetriever, run_arm, to_sessions
from emlab import runner


class Fixture(unittest.TestCase):
    def setUp(self):
        self.qs = list(FixtureLoader().questions())

    def test_shape(self):
        self.assertEqual(len(self.qs), 3)
        self.assertEqual(sum(len(q.session_ids) for q in self.qs), 12)
        ku = [q for q in self.qs if q.qtype == "knowledge-update"][0]
        self.assertEqual(ku.gold_session_ids, ["ku_s4"])
        self.assertEqual(ku.stale_session_ids, ["ku_s1"])
        ab = [q for q in self.qs if q.is_abstention][0]
        self.assertEqual(ab.gold_session_ids, [])

    def test_time_aware_filter_excludes_future_turns(self):
        q = self.qs[0]
        import datetime
        q2 = type(q)(**{**q.__dict__, "question_date": datetime.datetime(2023, 3, 1)})
        ranked = run_arm(FakeRetriever(), q2, "bm25", 50, True)
        self.assertNotIn("ku_s4:0", ranked)
        self.assertIn("ku_s4:0", run_arm(FakeRetriever(), q2, "bm25", 50, False))

    def test_session_ranking_dedupes_by_best_turn(self):
        self.assertEqual(to_sessions(["s2:1", "s1:0", "s2:0"]), ["s2", "s1"])

    def test_split_is_deterministic_and_order_independent(self):
        items = [("q%d" % i, "t%d" % (i % 2)) for i in range(20)]
        a = split_pilot_test(items)
        b = split_pilot_test(list(reversed(items)))
        self.assertEqual(a, b)
        self.assertEqual(sum(v == "pilot" for v in a.values()), 4)
        self.assertNotEqual(a, split_pilot_test(items, seed=1))

    def test_dry_run_runs_and_live_is_refused(self):
        self.assertEqual(runner.main(["--dry-run"]), 0)
        self.assertEqual(runner.main([]), 2)


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
    def test_time_filter_only_when_time_aware(self):
        self.assertEqual(len(bm25_query("x", "q1", 10)["query"]["bool"]["filter"]), 1)
        self.assertEqual(len(bm25_query("x", "q1", 10, "2023-01-01T00:00")["query"]["bool"]["filter"]), 2)
        self.assertIn("filter", knn_query([0.1], "q1", 5)["knn"])

    def test_mapping_vector_optional(self):
        self.assertNotIn("vector", build_mapping(None)["mappings"]["properties"])
        self.assertEqual(build_mapping(384)["mappings"]["properties"]["vector"]["dims"], 384)


if __name__ == "__main__":
    unittest.main()
