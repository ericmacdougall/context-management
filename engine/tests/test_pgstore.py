"""Needs the engine Postgres (run on the host: python -m unittest engine.tests.test_pgstore)."""
import time
import unittest
import uuid

from engine import keys, pgstore
from engine.common import pg_connect

T = "t1"


class PgStoreTests(unittest.TestCase):
    def setUp(self):
        self.sid = "ut" + uuid.uuid4().hex[:8]

    def test_append_once_is_exactly_once(self):
        k = f"{T}/k/{self.sid}"
        a, c1 = pgstore.append_once(T, k, self.sid, "x", "turn_result", {"v": 1})
        b, c2 = pgstore.append_once(T, k, self.sid, "x", "turn_result", {"v": 2})
        self.assertEqual((a, c1, c2), (b, True, False))
        self.assertEqual(len(pgstore.events_of(T, self.sid)), 1)
        self.assertTrue(pgstore.verify_session_chain(T, self.sid)[0])

    def test_key_without_the_tenant_is_refused(self):
        with self.assertRaises(ValueError):
            pgstore.append_once(T, f"other/{self.sid}", self.sid, "x", "k", {})

    def test_effect_retry_applies_once_but_broken_keys_apply_twice(self):
        ev, _ = pgstore.append_input(T, self.sid, "r1", {"n": 1})
        for _ in range(3):
            pgstore.external_effect(T, self.sid, ev, "t")
        with pg_connect() as c:
            applied = c.execute("select count(*) from effect_ledger where tenant_id=%s and ref_event=%s", (T, ev)).fetchone()[0]
            calls = c.execute("select count(*) from ext_calls where tenant_id=%s and key like %s", (T, f"{T}/eff/{ev}%")).fetchone()[0]
        self.assertEqual((applied, calls), (1, 3))
        ev2, _ = pgstore.append_input(T, self.sid, "r2", {"n": 2})
        for att in (1, 2):
            pgstore.external_effect(T, self.sid, ev2, "t", broken_keys=True, attempt=att)
        with pg_connect() as c:
            applied2 = c.execute("select count(*) from effect_ledger where tenant_id=%s and ref_event=%s", (T, ev2)).fetchone()[0]
        self.assertEqual(applied2, 2)  # the ledger comparison flags this duplicate effect

    def test_unprocessed_inputs_antijoin_in_seq_order(self):
        e1, _ = pgstore.append_input(T, self.sid, "r1", {"n": 1})
        e2, _ = pgstore.append_input(T, self.sid, "r2", {"n": 2})
        self.assertEqual(pgstore.unprocessed_inputs(T, self.sid), [e1, e2])
        pgstore.append_once(T, keys.result_key(T, e1), self.sid, "turn_result", "turn_result", {"in": e1})
        self.assertEqual(pgstore.unprocessed_inputs(T, self.sid), [e2])

    def test_dead_letter_counts_as_processed_even_for_an_erased_subject(self):
        e1, _ = pgstore.append_input(T, self.sid, "r1", {"n": 1})
        with pg_connect(autocommit=True) as c:
            c.execute("update keys set key=null, destroyed_at=now() where tenant_id=%s and subject_id=%s",
                      (T, pgstore.subject_of(self.sid)))
        with self.assertRaises(pgstore.SubjectErased):
            pgstore.read_digest(T, self.sid, e1)
        pgstore.dead_letter(T, self.sid, e1, "SubjectErased")          # no event can be written: effects row only
        self.assertEqual(pgstore.unprocessed_inputs(T, self.sid), [])

    def test_approval_happy_path_and_request_binding(self):
        r, dl = pgstore.request_approval(T, self.sid, "a1", 60000)
        self.assertEqual(pgstore.resolve_approval(T, self.sid, r), "pending")
        self.assertEqual(pgstore.grant_approval(T, self.sid, r, "approver-1"), "granted")
        self.assertEqual(pgstore.grant_approval(T, self.sid, r, "approver-2"), "duplicate")
        self.assertEqual(pgstore.resolve_approval(T, self.sid, r), "approval_granted")

    def test_grant_recorded_after_the_deadline_by_a_bypass_is_ignored(self):
        r, _ = pgstore.request_approval(T, self.sid, "a1", 300)
        time.sleep(0.6)
        pgstore.append_once(T, keys.approval_grant_key(T, r), self.sid, "approval", "approval_grant",
                            {"request": r, "approver": "approver-1"})          # bypasses the grant API
        self.assertEqual(pgstore.resolve_approval(T, self.sid, r), "approval_timeout")


if __name__ == "__main__":
    unittest.main()
