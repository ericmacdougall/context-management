import math
import unittest

from emlab.metrics import (best_gold_rank, clustered_paired_bootstrap, is_low_overlap, ndcg_at_k,
                           recall_at_k, stratified_macro_bootstrap, wilson_interval)


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



class Macro(unittest.TestCase):
    rows = ([("KU", "k%d" % i, 0.0, 1.0) for i in range(2)] + [("TR", "t%d" % i, 0.5, 0.5) for i in range(8)])

    def test_macro_differs_from_pooled_and_is_equal_weight(self):
        pooled = clustered_paired_bootstrap([(c, a, b) for _, c, a, b in self.rows], n_boot=200, seed=1)
        macro = stratified_macro_bootstrap(self.rows, n_boot=200, seed=1)
        self.assertAlmostEqual(pooled["mean_diff"], 0.2)            # question-weighted: KU counts 2 of 10
        self.assertAlmostEqual(macro["macro_diff"], 0.5)            # equal weight: (1.0 + 0.0) / 2
        # identical diffs inside each stratum -> every resample gives the same macro value
        self.assertEqual((macro["lower_one_sided"], macro["upper_one_sided"]), (0.5, 0.5))
        self.assertEqual(macro["clusters_per_stratum"], {"'KU'": 2.0, "'TR'": 8.0})

    def test_resamples_within_stratum_and_is_seeded(self):
        rows = [("KU", "k%d" % i, 0.0, float(i % 2)) for i in range(6)] + [("TR", "t%d" % i, 0.0, 0.0) for i in range(6)]
        a = stratified_macro_bootstrap(rows, n_boot=300, seed=4)
        self.assertEqual(a, stratified_macro_bootstrap(rows, n_boot=300, seed=4))
        self.assertGreater(a["upper_one_sided"], a["lower_one_sided"])
        self.assertLessEqual(a["upper_one_sided"], 0.5)              # TR stratum stays at 0, KU at most 1


if __name__ == "__main__":
    unittest.main()


class Exploratory(unittest.TestCase):
    def test_derangement_has_no_fixed_point_and_is_seeded(self):
        from emlab.xp_common import derangement_list
        p = derangement_list(50, 7)
        self.assertEqual(sorted(p), list(range(50)))
        self.assertTrue(all(p[i] != i for i in range(50)))
        self.assertEqual(p, derangement_list(50, 7))
        self.assertNotEqual(p, derangement_list(50, 8))

    def test_cluster_wilson_uses_clusters_not_questions(self):
        from emlab.xp_common import cluster_wilson
        per = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 0.0}
        one_cluster = cluster_wilson(per, {"a": "X", "b": "X", "c": "X", "d": "Y"})   # clusters: X (rate 1), Y (rate 0)
        self.assertEqual((one_cluster["n_clusters"], one_cluster["n_questions"], one_cluster["rate"]), (2, 4, 0.5))
        four = cluster_wilson(per, {"a": "A", "b": "B", "c": "C", "d": "D"})
        self.assertEqual(four["n_clusters"], 4)
        self.assertGreater(four["wilson_low"], one_cluster["wilson_low"])           # more clusters, tighter bound
        self.assertLess(one_cluster["wilson_low"], 0.5)
