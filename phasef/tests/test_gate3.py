import unittest

from phasef import gate3 as G


def dec_for(pair, a1, a2, b1, b2):
    d = {}
    for run, s1, s2 in (("A", a1, a2), ("B", b1, b2)):
        t1, t2 = G.TAG[run]
        d[G.pk(pair, t1)] = {"label": s1}
        if s2 is not None:
            d[G.pk(pair, t2)] = {"final": s2}
    return d


class FinalDecisions(unittest.TestCase):
    P = ("aaa", "bbb")

    def label(self, *a):
        return G.final_decisions([{"pair": self.P}], dec_for(self.P, *a))[self.P]

    def test_link_needs_both_runs(self):
        self.assertEqual(self.label("replaces", "replaces", "replaces", "replaces"), "replaces")
        self.assertEqual(self.label("replaces", "replaces", "adds", None), "unrepeated")
        self.assertEqual(self.label("adds", None, "replaces", "replaces"), "adds")
        self.assertEqual(self.label("replaces", "none", "replaces", "replaces"), "none")

    def test_mixed_labels_become_pending(self):
        self.assertEqual(self.label("replaces", "replaces", "pending", "pending"), "pending")
        self.assertEqual(self.label("pending", "pending", "replaces", "replaces"), "pending")

    def test_missing_stage2_is_not_a_link(self):
        self.assertEqual(self.label("replaces", None, "replaces", "replaces"), "unverified")


if __name__ == "__main__":
    unittest.main()
