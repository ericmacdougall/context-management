import math
import unittest

from emlab.metrics import (best_gold_rank, clustered_paired_bootstrap, is_low_overlap, ndcg_at_k,
                           recall_at_k, wilson_interval)


class RecallNdcg(unittest.TestCase):
    ranked = ["a", "b", "c", "d", "e"]

    def test_recall_boundary_is_inclusive_of_rank_k(self):
        # gold at rank 3: k=2 must miss, k=3 must hit (an off-by-one slice breaks one of these)
        self.assertEqual(recall_at_k(self.ranked, ["c"], 2), 0.0)
        self.assertEqual(recall_at_k(self.ranked, ["c"], 3), 1.0)

    def test_recall_partial_and_duplicates(self):
        self.assertEqual(recall_at_k(["a", "a", "b"], ["a", "x"], 3), 0.5)

    def test_no_gold_is_none_not_zero(self):
        self.assertIsNone(recall_at_k(self.ranked, [], 5))
        self.assertIsNone(ndcg_at_k(self.ranked, [], 5))

    def test_ndcg_hand_computed(self):
        # gold {a,c}: DCG = 1/log2(2) + 1/log2(4) = 1.5 ; IDCG = 1 + 1/log2(3)
        want = 1.5 / (1 + 1 / math.log2(3))
        self.assertAlmostEqual(ndcg_at_k(self.ranked, ["a", "c"], 5), want, places=12)
        self.assertEqual(ndcg_at_k(self.ranked, ["a"], 5), 1.0)
        self.assertAlmostEqual(ndcg_at_k(self.ranked, ["b"], 5), 1 / math.log2(3), places=12)

    def test_ndcg_duplicate_gain_counted_once(self):
        self.assertEqual(ndcg_at_k(["a", "a", "a"], ["a"], 3), 1.0)

    def test_best_gold_rank_and_low_overlap(self):
        self.assertEqual(best_gold_rank(self.ranked, ["d", "e"]), 4)
        self.assertIsNone(best_gold_rank(self.ranked, ["z"]))
        self.assertTrue(is_low_overlap(101, None))
        self.assertFalse(is_low_overlap(100, 500))  # rank exactly 100 is not "below 100"
        self.assertFalse(is_low_overlap(500, 3))    # must be low on BOTH


class Wilson(unittest.TestCase):
    def test_known_value_8_of_10(self):
        lo, hi = wilson_interval(8, 10)
        self.assertAlmostEqual(lo, 0.4902, places=3)
        self.assertAlmostEqual(hi, 0.9433, places=3)

    def test_edges(self):
        lo, hi = wilson_interval(0, 10)
        self.assertAlmostEqual(lo, 0.0, places=12)
        self.assertAlmostEqual(hi, 0.2775, places=3)
        lo, hi = wilson_interval(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=3)
        self.assertAlmostEqual(hi, 1.0, places=12)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            wilson_interval(1, 0)
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)


class Bootstrap(unittest.TestCase):
    rows = [("c%d" % (i % 6), 0.2 * (i % 3), 0.2 * (i % 3) + 0.1 + 0.05 * (i % 4)) for i in range(24)]

    def test_deterministic_under_seed_and_differs_across_seeds(self):
        a = clustered_paired_bootstrap(self.rows, n_boot=500, seed=7)
        b = clustered_paired_bootstrap(self.rows, n_boot=500, seed=7)
        c = clustered_paired_bootstrap(self.rows, n_boot=500, seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual((a["ci_low"], a["ci_high"]), (c["ci_low"], c["ci_high"]))

    def test_effective_n_is_clusters_not_questions(self):
        r = clustered_paired_bootstrap(self.rows, n_boot=100, seed=1)
        self.assertEqual(r["effective_n"], 6.0)
        self.assertEqual(r["questions"], 24.0)

    def test_observed_mean_and_interval_contains_it(self):
        r = clustered_paired_bootstrap(self.rows, n_boot=2000, seed=3)
        want = sum(b - a for _, a, b in self.rows) / len(self.rows)
        self.assertAlmostEqual(r["mean_diff"], want, places=12)
        self.assertLessEqual(r["ci_low"], want)
        self.assertGreaterEqual(r["ci_high"], want)

    def test_one_sided_bounds_are_inside_two_sided(self):
        r = clustered_paired_bootstrap(self.rows, n_boot=2000, seed=5)
        self.assertGreaterEqual(r["lower_one_sided"], r["ci_low"])
        self.assertLessEqual(r["upper_one_sided"], r["ci_high"])
        self.assertLess(r["lower_one_sided"], r["mean_diff"])

    def test_zero_difference_interval_is_degenerate(self):
        r = clustered_paired_bootstrap([("x", 0.5, 0.5), ("y", 0.1, 0.1)], n_boot=50, seed=0)
        self.assertEqual((r["ci_low"], r["ci_high"], r["mean_diff"]), (0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
