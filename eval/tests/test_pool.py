import unittest

from emlab.corpus import cluster_ids
from emlab.dataset import FixtureLoader
from emlab.pool import (cluster_haystacks, contamination_proxy, distribution, level_session_ids,
                        pool_universe)
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

    def test_shared_sets_nested_exclude_split_haystacks_and_deterministic(self):
        sets = pool.shared_distractors(self.uni, [self.qs[0]], "pilot")
        used = set(self.qs[0].haystack_session_ids)
        self.assertFalse(used & set(sets["D5000"]))
        self.assertEqual(sets["eligible"], len(self.uni) - len(used))
        self.assertTrue(sets["d5000_short"])  # fixture pool is tiny: D5000 is every eligible session
        self.assertEqual(sets["D5000"][:len(sets["D500"])], sets["D500"])
        self.assertEqual(sets, pool.shared_distractors(self.uni, [self.qs[0]], "pilot"))

    def test_levels_share_sets_across_questions_and_nest(self):
        split = [self.qs[0], self.qs[1]]
        sets = pool.shared_distractors(self.uni, split, "pilot")
        sets = {**sets, "D500": sets["D5000"][:2]}  # shrink D500 so nesting is visible on the fixture
        for q in split:
            cs = self.ch[self.cl[q.qid]]
            lv = {l: level_session_ids(q, self.uni, cs, l, sets) for l in pool.LEVELS}
            own = q.haystack_session_ids
            self.assertEqual(lv["H0"], own)
            self.assertEqual(lv["H1"], own + sets["D500"])
            self.assertEqual(lv["H2"], own + sets["D5000"])
            self.assertTrue(set(lv["H1"]) < set(lv["H2"]) <= set(lv["H3"]))
            self.assertFalse(set(lv["H3"]) - set(own) & set(cs))      # no own-cluster sessions beyond own
            self.assertEqual(len(lv["H3"]), len(set(lv["H3"])))
        a = level_session_ids(split[0], self.uni, self.ch[self.cl[split[0].qid]], "H2", sets)
        b = level_session_ids(split[1], self.uni, self.ch[self.cl[split[1].qid]], "H2", sets)
        self.assertEqual(a[len(split[0].haystack_session_ids):], b[len(split[1].haystack_session_ids):])  # shared

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
