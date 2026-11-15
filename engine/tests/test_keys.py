import unittest

from engine import keys

T = "t1"


def attempt_independent(fn):
    """A key function must return the same key for the same logical step whatever the attempt/run/worker."""
    return all(fn("t1/s1/7", attempt=a, run=r) == fn("t1/s1/7", attempt=0, run="r0") for a in range(4) for r in ("r1", "r2"))


def good(eid, attempt, run):
    return keys.effect_key(T, eid)


def broken_attempt(eid, attempt, run):
    return f"{keys.effect_key(T, eid)}/a{attempt}"


def broken_run(eid, attempt, run):
    return f"{keys.effect_key(T, eid)}/{run}"


def tenantless(sid_):
    """The pre-fix shape: no tenant in the key."""
    return f"finish/{sid_}"


class KeyTests(unittest.TestCase):
    def test_real_derivation_is_attempt_independent(self):
        self.assertTrue(attempt_independent(good))

    def test_property_check_catches_broken_variants(self):
        self.assertFalse(attempt_independent(broken_attempt))
        self.assertFalse(attempt_independent(broken_run))

    def test_distinct_steps_distinct_keys(self):
        ks = {keys.effect_key(T, "t1/s1/1"), keys.result_key(T, "t1/s1/1"), keys.effect_key(T, "t1/s1/2"),
              keys.rollup_key(T, "s1", 1000), keys.rollup_key(T, "s1", 2000), keys.approval_request_key(T, "s1", "a1"),
              keys.approval_grant_key(T, "t1/s1/5"), keys.approval_resolve_key(T, "t1/s1/5"), keys.finish_key(T, "s1"),
              keys.start_key(T, "s1"), keys.input_key(T, "s1", "r1")}
        self.assertEqual(len(ks), 11)

    def test_every_key_carries_the_tenant_and_tenants_never_collide(self):
        for t in ("t1", "tB"):
            for k in (keys.effect_key(t, f"{t}/s/1"), keys.result_key(t, f"{t}/s/1"), keys.rollup_key(t, "s", 5),
                      keys.approval_request_key(t, "s", "a"), keys.approval_grant_key(t, f"{t}/s/2"),
                      keys.approval_resolve_key(t, f"{t}/s/2"), keys.finish_key(t, "s"), keys.start_key(t, "s"),
                      keys.input_key(t, "s", "r")):
                self.assertTrue(k.startswith(t + "/"))
        self.assertNotEqual(keys.finish_key("t1", "s"), keys.finish_key("tB", "s"))
        self.assertEqual(tenantless("s"), tenantless("s"))   # the old shape: identical for every tenant (the bug)

    def test_rollup_key_depends_on_the_deadline_not_a_counter(self):
        self.assertNotEqual(keys.rollup_key(T, "s", 1000), keys.rollup_key(T, "s", 2000))

    def test_bad_tenant_and_bad_approval_ids_rejected(self):
        for bad in ("", "a/b", "x" * 41, None, "a b"):
            with self.assertRaises(ValueError):
                keys.finish_key(bad, "s")

    def test_event_id_belongs_only_to_its_tenant_and_session(self):
        self.assertTrue(keys.belongs_to("t1", "s1", "t1/s1/7"))
        self.assertFalse(keys.belongs_to("t1", "s1", "t1/s2/7"))     # other session
        self.assertFalse(keys.belongs_to("t1", "s1", "tB/s1/7"))     # other tenant
        for bad in ("t1/s1", "t1/s1/x", "t1/s1/7/9", "", None, "t1//7"):
            self.assertFalse(keys.belongs_to("t1", "s1", bad))


if __name__ == "__main__":
    unittest.main()
