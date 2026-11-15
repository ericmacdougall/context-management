import unittest

from engine import keys


def attempt_independent(fn):
    """A key function must return the same key for the same logical step whatever the attempt/run/worker."""
    return all(fn("t1/s1/7", attempt=a, run=r) == fn("t1/s1/7", attempt=0, run="r0") for a in range(4) for r in ("r1", "r2"))


def good(eid, attempt, run):
    return keys.effect_key(eid)


def broken_attempt(eid, attempt, run):
    return f"{keys.effect_key(eid)}/a{attempt}"


def broken_run(eid, attempt, run):
    return f"{keys.effect_key(eid)}/{run}"


class KeyTests(unittest.TestCase):
    def test_real_derivation_is_attempt_independent(self):
        self.assertTrue(attempt_independent(good))

    def test_property_check_catches_broken_variants(self):
        self.assertFalse(attempt_independent(broken_attempt))
        self.assertFalse(attempt_independent(broken_run))

    def test_distinct_steps_distinct_keys(self):
        ks = {keys.effect_key("t1/s1/1"), keys.result_key("t1/s1/1"), keys.effect_key("t1/s1/2"),
              keys.rollup_key("s1", 1), keys.rollup_key("s1", 2), keys.approval_key("s1", "grant"),
              keys.approval_key("s1", "request"), keys.finish_key("s1"), keys.start_key("s1")}
        self.assertEqual(len(ks), 9)

    def test_rejects_unknown_approval_step(self):
        with self.assertRaises(ValueError):
            keys.approval_key("s1", "other")


if __name__ == "__main__":
    unittest.main()
