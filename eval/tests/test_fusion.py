import unittest

from emlab.fusion import linear, rrf


class Rrf(unittest.TestCase):
    def test_hand_computed_k1(self):
        # k=1, ranks 1-based: d1 = 1/2+1/4 = .75 ; d2 = 1/3+1/2 = .8333 ; d3 = 1/4+1/3 = .5833
        out = rrf([["d1", "d2", "d3"], ["d2", "d3", "d1"]], k=1)
        self.assertEqual([d for d, _ in out], ["d2", "d1", "d3"])
        sc = dict(out)
        self.assertAlmostEqual(sc["d1"], 0.75, places=12)
        self.assertAlmostEqual(sc["d2"], 5 / 6, places=12)
        self.assertAlmostEqual(sc["d3"], 7 / 12, places=12)

    def test_k60_default_order(self):
        out = rrf([["d1", "d2", "d3"], ["d2", "d3", "d1"]])
        self.assertEqual([d for d, _ in out], ["d2", "d1", "d3"])
        self.assertAlmostEqual(dict(out)["d1"], 1 / 61 + 1 / 63, places=12)

    def test_doc_in_one_list_only_and_tie_break_is_stable(self):
        out = rrf([["a"], ["b"]])
        self.assertEqual([d for d, _ in out], ["a", "b"])  # equal score, first-seen wins
        self.assertEqual(out, rrf([["a"], ["b"]]))

    def test_weights(self):
        out = rrf([["a", "b"], ["b", "a"]], k=1, weights=[1.0, 3.0])
        self.assertEqual(out[0][0], "b")

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            rrf([["a"]], weights=[1, 2])


class Linear(unittest.TestCase):
    def test_minmax_then_sum(self):
        out = linear([{"a": 10.0, "b": 0.0}, {"b": 5.0, "c": 1.0}])
        # list1 norm: a=1,b=0 ; list2 norm: b=1,c=0 -> a=1, b=1, c=0 ; tie a/b broken by repr
        self.assertEqual([d for d, _ in out], ["a", "b", "c"])
        self.assertEqual(dict(out), {"a": 1.0, "b": 1.0, "c": 0.0})

    def test_weights_change_order(self):
        out = linear([{"a": 1.0, "b": 0.0}, {"b": 1.0, "a": 0.0}], weights=[1.0, 2.0])
        self.assertEqual(out[0][0], "b")


if __name__ == "__main__":
    unittest.main()
