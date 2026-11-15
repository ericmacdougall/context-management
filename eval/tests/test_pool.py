import unittest

from emlab.corpus import cluster_ids
from emlab.dataset import FixtureLoader
from emlab.pool import (cluster_haystacks, contamination_proxy, distribution, level_session_ids,
                        pool_universe, with_sessions)
import emlab.pool as pool


class Stub:
    def __init__(self, bm, de):
        self.bm, self.de = bm, de

    def bm25(self, q, depth, allowed):
        return [(r, 1.0) for r in self.bm]

    def dense(self, q, depth, allowed):
        return [(r, 1.0) for r in self.de]


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.qs = list(FixtureLoader().questions())
        self.cl = cluster_ids(self.qs)
        self.ch = cluster_haystacks(self.qs, self.cl)
        self.uni = pool_universe(self.qs)
        self.q = self.qs[0]

    def test_levels_nested_and_exclude_cluster_sessions(self):
        old = pool.LEVEL_SIZES
        pool.LEVEL_SIZES = (("H0", 0), ("H1", 2), ("H2", 5), ("H3", None))
        try:
            lv = {l: level_session_ids(self.q, self.uni, self.ch[self.cl[self.q.qid]], l) for l, _ in pool.LEVEL_SIZES}
        finally:
            pool.LEVEL_SIZES = old
        self.assertEqual(lv["H0"], self.q.haystack_session_ids)
        self.assertEqual(lv["H1"][: len(lv["H0"])], lv["H0"])
        self.assertTrue(set(lv["H1"]) < set(lv["H2"]) < set(lv["H3"]))
        self.assertEqual(lv["H2"][: len(lv["H1"])], lv["H1"])  # nested prefix
        self.assertEqual(set(lv["H3"]), set(self.uni))          # whole pool
        extra = set(lv["H3"]) - set(lv["H0"])
        self.assertFalse(extra & set(self.ch[self.cl[self.q.qid]]))
        self.assertEqual(len(lv["H3"]), len(set(lv["H3"])))

    def test_deterministic_and_question_specific(self):
        a = level_session_ids(self.q, self.uni, self.ch[self.cl[self.q.qid]], "H3")
        b = level_session_ids(self.q, self.uni, self.ch[self.cl[self.q.qid]], "H3")
        self.assertEqual(a, b)
        other = self.qs[2]
        c = level_session_ids(other, self.uni, self.ch[self.cl[other.qid]], "H3")
        self.assertNotEqual(a[len(self.q.haystack_session_ids):], c[len(other.haystack_session_ids):])

    def test_contamination_counts_only_rounds_above_gold_on_both(self):
        q = self.qs[2]  # gold round lo_s2:0
        g = q.retrieval_gold_round_ids[0]
        r = Stub(["x1", "x2", g, "x3"], ["x2", "x4", "x5", g])
        c = contamination_proxy(q, r)
        self.assertEqual(c["above_on_both"], 1)   # only x2 outranks gold on both legs
        self.assertEqual(c["above_bm25"], 2)
        self.assertEqual(c["above_dense"], 3)
        self.assertIsNone(contamination_proxy(self.qs[1], r))  # abstention

    def test_distribution(self):
        d = distribution([0, 0, 3, 9])
        self.assertEqual((d["n"], d["min"], d["max"], d["n_zero"]), (4, 0, 9, 2))


if __name__ == "__main__":
    unittest.main()
