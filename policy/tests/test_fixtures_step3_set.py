"""The committed step-3 fixture set (fixtures/step3, exported by exp/fixtures_step3.py at the commit its MANIFEST.json names):
- it matches its manifest and replays clean through the oracle pol._core;
- every B1 control's mutated oracle is caught by it, so a clean replay is not vacuous;
- on Postgres (the environment of tests/test_pg.py) it replays clean through the dc core, the reference the port replaces, and
  pg_diff's mutated core (stale check removed) is caught. Without that environment CommittedSetPg FAILS (review of 2262eaa: it was a
  silent skip), unless POL_STEP3_SKIP_PG=1 is set explicitly; then each test is SKIPPED with that reason and counted as a skip.
The port into the write path must pass the same replay against its own schema (HANDOFF-STEP3.md)."""
import os
import sys
import unittest

from exp import b1_modelcheck as B1
from exp import fixtures_step3 as F
from tests.test_pg import ENV

try:
    import psycopg  # noqa: F401
except ImportError:              # pragma: no cover
    psycopg = None

SET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "step3")


class CommittedSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.setrecursionlimit(10000)

    def test_matches_manifest_and_oracle(self):
        rep = F.Report()
        man = F.check_manifest(SET, rep)
        F.check_oracle(SET, None, rep)
        self.assertEqual((rep.mismatches, rep.first), (0, []))
        self.assertEqual(rep.counts["b2_events"], man["b2"]["events"])
        self.assertEqual(rep.counts["b1_scenarios"], len(B1.DEPTH))           # every B1 scenario is in the set
        self.assertFalse(str(man["commit"]).startswith("dirty"), man["commit"])

    def test_every_b1_control_is_caught(self):
        for control in B1.CTL_BY_NAME:
            with self.subTest(control=control):
                self.assertGreater(F.check_oracle(SET, control).mismatches, 0)


class CommittedSetPg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.setrecursionlimit(10000)

    def setUp(self):
        if psycopg is None or not all(os.environ.get(k) for k in ENV):
            if os.environ.get("POL_STEP3_SKIP_PG") == "1":
                self.skipTest("POL_STEP3_SKIP_PG=1: the Postgres replay of the step-3 set was NOT run")
            self.fail("Postgres lab environment not configured (tests/test_pg.py ENV); set POL_STEP3_SKIP_PG=1 to skip explicitly")

    def test_dc_core_replays_clean(self):
        rep = F.check_pg(SET)
        self.assertEqual((rep.mismatches, rep.first), (0, []))
        self.assertEqual(rep.counts["b1_scenarios"], len(B1.DEPTH))

    def test_mutated_dc_core_is_caught(self):
        from exp.pg_diff import STALE_NEW, STALE_OLD
        self.assertGreater(F.check_pg(SET, patches=[(STALE_OLD, STALE_NEW)]).mismatches, 0)


if __name__ == "__main__":
    unittest.main()
