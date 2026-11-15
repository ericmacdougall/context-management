import unittest

from engine import state


def broken_migrate(d):  # forgets the rename: loses the counter
    d = dict(d)
    d["ver"] = 2
    d.setdefault("rollup_count", 0)
    d.setdefault("policy_gen", 0)
    return d


class StateTests(unittest.TestCase):
    def v1(self):
        return state.new_state("t1", "s1", ver=1, rollups=3, turns_done=9, pend=["t1/s1/4"])

    def test_v1_to_v2_renames_and_adds(self):
        d = state.migrate(self.v1())
        self.assertEqual(d["ver"], 2)
        self.assertEqual(d["rollup_count"], 3)
        self.assertNotIn("rollups", d)
        self.assertEqual(d["policy_gen"], 0)
        self.assertEqual(d["last_result"], "")
        self.assertEqual(d["turns_done"], 9)
        self.assertEqual(d["pend"], ["t1/s1/4"])  # unknown-to-migration fields survive

    def test_idempotent_and_pure(self):
        v1 = self.v1()
        before = dict(v1)
        a = state.migrate(v1)
        self.assertEqual(v1, before)
        self.assertEqual(state.migrate(a), a)

    def test_future_state_refused(self):
        with self.assertRaises(state.StateFromFuture):
            state.migrate({"ver": 3})

    def test_new_state_v2(self):
        self.assertEqual(state.new_state("t1", "s1")["ver"], 2)

    def test_broken_variant_is_detected_by_the_same_assertions(self):
        d = broken_migrate(self.v1())
        self.assertNotEqual(d["rollup_count"], 3)   # the counter was lost
        self.assertIn("rollups", d)                 # and the old name was left behind


if __name__ == "__main__":
    unittest.main()
