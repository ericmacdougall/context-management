"""Needs the engine Postgres (run on the host: python -m unittest engine.tests.test_pgstore)."""
import unittest
import uuid

from engine import pgstore
from engine.common import pg_connect


class PgStoreTests(unittest.TestCase):
    def setUp(self):
        self.sid = "ut" + uuid.uuid4().hex[:8]

    def test_append_once_is_exactly_once(self):
        a, c1 = pgstore.append_once(f"k/{self.sid}", self.sid, "x", "turn_result", {"v": 1})
        b, c2 = pgstore.append_once(f"k/{self.sid}", self.sid, "x", "turn_result", {"v": 2})
        self.assertEqual((a, c1, c2), (b, True, False))
        self.assertEqual(len(pgstore.events_of(self.sid)), 1)
        self.assertTrue(pgstore.verify_session_chain(self.sid)[0])

    def test_effect_retry_applies_once_but_broken_keys_apply_twice(self):
        ev = pgstore.append(self.sid, "turn_input", {"n": 1})
        for _ in range(3):
            pgstore.external_effect(self.sid, ev, "t")
        with pg_connect() as c:
            applied = c.execute("select count(*) from effect_ledger where ref_event=%s", (ev,)).fetchone()[0]
            calls = c.execute("select count(*) from ext_calls where key like %s", (f"eff/{ev}%",)).fetchone()[0]
        self.assertEqual((applied, calls), (1, 3))
        ev2 = pgstore.append(self.sid, "turn_input", {"n": 2})
        for att in (1, 2):
            pgstore.external_effect(self.sid, ev2, "t", broken_keys=True, attempt=att)
        with pg_connect() as c:
            applied2 = c.execute("select count(*) from effect_ledger where ref_event=%s", (ev2,)).fetchone()[0]
        self.assertEqual(applied2, 2)  # the ledger comparison would flag this duplicate effect

    def test_unprocessed_inputs_antijoin(self):
        e1 = pgstore.append(self.sid, "turn_input", {"n": 1})
        e2 = pgstore.append(self.sid, "turn_input", {"n": 2})
        self.assertEqual(pgstore.unprocessed_inputs(self.sid), [e1, e2])
        pgstore.append_once(f"res/{e1}", self.sid, "turn_result", "turn_result", {"in": e1})
        self.assertEqual(pgstore.unprocessed_inputs(self.sid), [e2])


if __name__ == "__main__":
    unittest.main()
