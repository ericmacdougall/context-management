import unittest

from phasef.provenance import anchor, author_verdict, rounds_of_turns, wilson

TURNS = [{"role": "user", "content": "I keep my  sneakers\nunder the bed"}, {"role": "assistant", "content": "Try a shoe rack. I keep my sneakers under the bed too."},
         {"role": "user", "content": "Thanks, I will buy a rack"}, {"role": "assistant", "content": "Great."}]


class Prov(unittest.TestCase):
    def test_rounds_start_at_user_turns(self):
        g = rounds_of_turns(TURNS)
        self.assertEqual([len(x) for x in g], [2, 2])
        self.assertEqual(len(rounds_of_turns([{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])), 2)

    def test_anchor_normalises_whitespace_and_case_only_and_rejects_near_misses(self):
        g = rounds_of_turns(TURNS)
        self.assertTrue(anchor("i keep my SNEAKERS under the bed", g, 0)[0])
        self.assertFalse(anchor("i keep my sneakers under the couch", g, 0)[0])  # one word changed
        self.assertFalse(anchor("I will buy a rack", g, 0)[0])  # adjacent round
        self.assertFalse(anchor("I will buy a rack", g, 5)[0])  # no such round
        self.assertFalse(anchor("", g, 0)[0])

    def test_author_verdicts_including_ambiguous_not_a_pass(self):
        g = rounds_of_turns(TURNS)
        ok, sp = anchor("I keep my sneakers under the bed", g, 0)
        self.assertEqual(sp, {"user", "assistant"})
        self.assertEqual(author_verdict("user", sp), "ambiguous")
        ok, sp = anchor("Try a shoe rack", g, 0)
        self.assertEqual((author_verdict("assistant", sp), author_verdict("user", sp)), ("pass", "mismatch"))
        self.assertEqual(author_verdict("user", set()), "undefined")

    def test_wilson(self):
        lo, hi = wilson(95, 100)
        self.assertTrue(0.88 < lo < 0.9 and 0.97 < hi < 0.99)


if __name__ == "__main__":
    unittest.main()
