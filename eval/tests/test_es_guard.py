import unittest

from emlab.es_guard import EsGuardError, GuardedEs
from emlab.escompare import set_heuristic

IDX = "emlab-rounds-v1"


class Fake:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, ndjson=False):
        self.calls.append((method, path))
        if path.endswith("_search"):
            return {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0}, "hits": {"hits": []}}
        return {IDX: {"settings": {"index": {"dense_vector": {"hnsw_filter_heuristic": "acorn"}}}}}


class Guard(unittest.TestCase):
    def test_prior_set_heuristic_would_write_through_a_plain_client(self):
        f = Fake()
        set_heuristic(f, IDX, "fanout")  # the prior pilot's call path: a PUT of index settings reaches the node
        self.assertIn(("PUT", "/%s/_settings" % IDX), f.calls)

    def test_guard_refuses_the_settings_write_before_the_inner_client_sees_it(self):
        f = Fake()
        g = GuardedEs(f, IDX)
        with self.assertRaises(EsGuardError):
            set_heuristic(g, IDX, "fanout")
        self.assertEqual(f.calls, [])

    def test_refused_requests(self):
        f = Fake()
        g = GuardedEs(f, IDX)
        for m, p in (("PUT", "/%s/_settings" % IDX), ("DELETE", "/" + IDX), ("POST", "/%s/_bulk" % IDX), ("POST", "/%s/_doc" % IDX),
                     ("POST", "/%s/_update_by_query" % IDX), ("POST", "/%s/_delete_by_query" % IDX), ("POST", "/_search"),
                     ("GET", "/other-index/_search"), ("POST", "/other-index/_search"), ("PUT", "/" + IDX), ("POST", "/_cluster/settings"),
                     ("POST", "/%s/_refresh" % IDX), ("POST", "/%s/_forcemerge" % IDX), ("POST", "/%s/_close" % IDX),
                     ("GET", "/_cluster/settings"), ("GET", "/_security/user")):
            with self.assertRaises(EsGuardError, msg=(m, p)):
                g.request(m, p, {})
        with self.assertRaises(EsGuardError):
            g.request("POST", "/%s/_search" % IDX, {"pit": {"id": "x"}})
        with self.assertRaises(EsGuardError):
            g.request("POST", "/%s/_bulk" % IDX, [{}], ndjson=True)
        self.assertEqual(f.calls, [])
        self.assertEqual(g.log["REFUSED"], 16)

    def test_allowed_requests_pass_and_are_counted(self):
        f = Fake()
        g = GuardedEs(f, IDX)
        g.request("GET", "/_cluster/health")
        g.request("GET", "/%s/_settings" % IDX)
        g.request("GET", "/%s/_mapping" % IDX)
        g.request("GET", "/%s/_count" % IDX)
        g.request("GET", "/_cat/indices/%s?format=json" % IDX)
        g.request("GET", "/_nodes/stats/thread_pool")
        g.search(IDX, {"query": {"match_all": {}}})
        self.assertEqual(len(f.calls), 7)
        self.assertEqual(g.log["POST search"], 1)
        self.assertEqual(g.search_flags["n"], 1)

    def test_flags_count_timeouts_and_failed_shards(self):
        class Bad(Fake):
            def request(self, method, path, body=None, ndjson=False):
                return {"timed_out": True, "_shards": {"total": 1, "successful": 0, "failed": 1}, "hits": {"hits": []}}
        g = GuardedEs(Bad(), IDX)
        g.search(IDX, {})
        self.assertEqual((g.search_flags["timed_out"], g.search_flags["shards_failed"], g.search_flags["shards_incomplete"]), (1, 1, 1))

    def test_nested_pit_or_script_is_refused(self):
        f = Fake()
        g = GuardedEs(f, IDX)
        for body in ({"query": {"script_score": {"query": {"match_all": {}}, "script": {"source": "1"}}}},
                     {"query": {"bool": {"filter": [{"script": {"script": {"source": "true"}}}]}}},
                     {"knn": {"field": "vector", "script": {"source": "x"}}},
                     {"sub": [{"pit": {"id": "x"}}]}):
            with self.assertRaises(EsGuardError, msg=body):
                g.search(IDX, body)
        self.assertEqual(f.calls, [])
        g.search(IDX, {"query": {"match": {"text": {"query": "script pit"}}}})  # words in values are fine
        self.assertEqual(len(f.calls), 1)


if __name__ == "__main__":
    unittest.main()
