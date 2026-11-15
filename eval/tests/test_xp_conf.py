import unittest

try:
    import numpy as np
    from emlab.xp_conf import auroc, cluster_folds, cv_oof, fit_logistic, predict
except ImportError:  # numpy is only on the host venv
    np = None


@unittest.skipIf(np is None, "numpy not installed")
class Conf(unittest.TestCase):
    def test_auroc_hand_case_and_ties(self):
        self.assertAlmostEqual(auroc(np.array([1, 0, 1, 0]), np.array([0.9, 0.8, 0.7, 0.1])), 0.75)
        self.assertAlmostEqual(auroc(np.array([1, 0]), np.array([0.5, 0.5])), 0.5)       # tie counts half
        self.assertTrue(np.isnan(auroc(np.array([1, 1]), np.array([0.1, 0.2]))))

    def test_folds_are_grouped_by_cluster(self):
        q = ["a", "b", "c", "d", "e", "f"]
        cl = {"a": "X", "b": "X", "c": "Y", "d": "Y", "e": "Z", "f": "W"}
        f = cluster_folds(q, cl, 3)
        self.assertEqual(f[0], f[1])
        self.assertEqual(f[2], f[3])

    def test_logistic_separates_and_null_is_near_half(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(200, 2))
        y = (X[:, 0] + 0.1 * rng.normal(size=200) > 0).astype(float)
        folds = np.arange(200) % 5
        self.assertGreater(auroc(y, cv_oof(X, y, folds)), 0.95)
        yp = rng.permutation(y)
        self.assertLess(abs(auroc(yp, cv_oof(X, yp, folds)) - 0.5), 0.15)               # a broken label link must not look good
        w = fit_logistic((X - X.mean(0)) / X.std(0), y)
        self.assertGreater(w[1], 0)


if __name__ == "__main__":
    unittest.main()
