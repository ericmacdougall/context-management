import dataclasses
import unittest

from emlab.content import content_clusters, session_hashes
from emlab.dataset import FixtureLoader, Turn
from emlab.pool import PoolContext, contamination_proxy, distribution


def twin_of(q, new_qid, rename):
    """A copy of question q whose sessions are renamed (same content under new ids)."""
    turns = [dataclasses.replace(t, session_id=rename.get(t.session_id, t.session_id)) for t in q.turns]
    return dataclasses.replace(q, qid=new_qid, turns=turns,
                               haystack_session_ids=[rename.get(s, s) for s in q.haystack_session_ids],
                               gold_session_ids=[rename.get(s, s) for s in q.gold_session_ids])


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
        self.q = self.qs[0]

    def test_twin_sessions_share_a_hash_and_cluster_together(self):
        twin = twin_of(self.q, "ku_001_abs", {s: s + "_copy" for s in self.q.haystack_session_ids})
        allq = self.qs + [twin]
        h = session_hashes(allq)
        self.assertEqual(h["ku_s4"], h["ku_s4_copy"])
        cc = content_clusters(allq, h)
        self.assertEqual(cc["ku_001"], cc["ku_001_abs"])          # same gold content under other ids
        self.assertNotEqual(cc["ku_001"], cc["lo_003"])

    def test_abs_base_pair_clusters_even_without_shared_content(self):
        base = self.qs[1]  # ab_002_abs has no base in the fixture; make one
        b = dataclasses.replace(base, qid="ab_002", gold_session_ids=[])
        h = session_hashes(self.qs + [b])
        self.assertEqual(content_clusters(self.qs + [b], h)["ab_002"], content_clusters(self.qs + [b], h)["ab_002_abs"])

    def test_dedup_pool_and_gold_exclusion_and_own_twin_never_a_distractor(self):
        twin = twin_of(self.q, "x1", {s: s + "_copy" for s in self.q.haystack_session_ids})
        ctx = PoolContext(self.qs + [twin])
        self.assertLess(len(ctx.pool), len(ctx.hashes))            # twins collapse to one canonical id
        el = ctx.eligible([self.qs[1]])                           # a split whose haystack is ab_*
        own_h = {ctx.hashes[s] for s in self.qs[1].haystack_session_ids}
        self.assertFalse({ctx.hashes[s] for s in el} & own_h)      # no twin of the split's own content
        self.assertFalse({ctx.hashes[s] for s in el} & ctx.gold_hashes)  # no gold content for anyone
        sets = ctx.shared_distractors([self.qs[1]], "pilot", 3)
        lv = ctx.level_sessions(self.qs[1], "H2", sets)
        self.assertEqual(lv[:3], self.qs[1].haystack_session_ids)
        self.assertEqual(len(lv), len(set(ctx.hashes[s] for s in lv)))  # content-unique

    def test_h3_removes_the_content_cluster_and_twins_of_own_sessions(self):
        twin = twin_of(self.q, "ku_001_abs", {s: s + "_copy" for s in self.q.haystack_session_ids})
        ctx = PoolContext(self.qs + [twin])
        sets = ctx.shared_distractors([self.q], "pilot", 2)
        lv = ctx.level_sessions(self.q, "H3", sets)
        hs = {ctx.hashes[s] for s in lv}
        self.assertEqual(len(lv), len(hs))
        extra = set(lv) - set(self.q.haystack_session_ids)
        self.assertFalse({ctx.hashes[s] for s in extra} & ctx.cluster_hay[ctx.clusters[self.q.qid]])
        self.assertIn("lo_s1", lv)                                 # unrelated pool sessions are kept

    def test_matched_L_is_the_smaller_split_rounded_down(self):
        ctx = PoolContext(self.qs)
        m = ctx.matched_L([self.qs[0]], [self.qs[2]], step=1)
        self.assertEqual(m["L"], min(m["eligible_pilot"], m["eligible_test"]))

    def test_contamination_counts_only_rounds_above_gold_on_both(self):
        q = self.qs[2]
        g = q.retrieval_gold_round_ids[0]
        c = contamination_proxy(q, Stub(["x1", "x2", g, "x3"], ["x2", "x4", "x5", g]))
        self.assertEqual((c["above_on_both"], c["above_bm25"], c["above_dense"]), (1, 2, 3))
        self.assertIsNone(contamination_proxy(self.qs[1], Stub([], [])))

    def test_distribution(self):
        d = distribution([0, 0, 3, 9])
        self.assertEqual((d["n"], d["min"], d["max"], d["n_zero"]), (4, 0, 9, 2))


if __name__ == "__main__":
    unittest.main()
