"""Review-fix tests (H1 H2 H3 M3 M4 M5). Need the engine Postgres: run on t2-dev with
`python -m unittest engine.tests.test_review_fixes` from a code directory. The same tests run on the old code
(62916ae) through tests/compat.py and fail there for the reasons the review gave."""
import time
import unittest

from engine.tests import compat as c

A, B = "t1", "tB"


class H1Tenant(unittest.TestCase):
    def test_other_tenants_grant_does_not_approve_my_gate(self):
        s = c.sid()                       # two tenants, the SAME session id
        past = c.now_ms() - 1000
        ra = c.request(A, s, "anchor-1", past)                      # tenant A: gate whose deadline has passed
        rb = c.request(B, s, "anchor-1", c.now_ms() + 60000)
        c.grant(B, s, rb)                                           # grant exists only in tenant B
        self.assertEqual(c.resolve(A, s, ra, past), "timeout")      # A must time out

    def test_finish_and_start_keys_are_per_tenant(self):
        s = c.sid()
        c.finish(A, s)
        c.finish(B, s)
        self.assertEqual(len(c.events(A, s, "session_finished")), 1)
        self.assertEqual(len(c.events(B, s, "session_finished")), 1)   # old: B's finish suppressed by A's key


class H2Approval(unittest.TestCase):
    def test_second_gate_resolves_against_its_own_request(self):
        s = c.sid()
        r1 = c.request(A, s, "anchor-1", c.now_ms() + 60000)
        c.grant(A, s, r1)
        self.assertEqual(c.resolve(A, s, r1, c.now_ms() + 60000), "granted")
        past = c.now_ms() - 1000
        r2 = c.request(A, s, "anchor-2", past)                      # second gate, deadline passed, never granted
        self.assertEqual(c.resolve(A, s, r2, past), "timeout")      # old: returns the first gate's resolution

    def test_grant_before_the_request_is_not_honoured(self):
        s = c.sid()
        c.grant(A, s, "t1/%s/999" % s)                              # no such request yet
        r = c.request(A, s, "anchor-1", c.now_ms() + 60000)
        self.assertEqual(c.resolve(A, s, r, c.now_ms() + 60000), "pending")

    def test_grant_after_the_deadline_is_not_honoured(self):
        s = c.sid()
        dl = c.now_ms() + 300
        r = c.request(A, s, "anchor-1", dl)
        time.sleep(0.6)
        c.grant(A, s, r)
        self.assertEqual(c.resolve(A, s, r, dl), "timeout")

    def test_grant_needs_an_approver(self):
        s = c.sid()
        r = c.request(A, s, "anchor-1", c.now_ms() + 60000)
        c.grant(A, s, r, approver="")
        self.assertEqual(c.resolve(A, s, r, c.now_ms() + 60000), "pending")


class H3ForeignEventId(unittest.TestCase):
    def test_reading_another_sessions_input_is_refused(self):
        victim, attacker = c.sid(), c.sid()
        ev = c.make_input(A, victim, "r1")
        with self.assertRaises(Exception):
            c.read_digest(A, attacker, ev)      # returning a digest of someone else's input is the bug

    def test_foreign_id_cannot_mark_the_victims_input_processed(self):
        victim, attacker = c.sid(), c.sid()
        ev = c.make_input(A, victim, "r1")
        try:
            c.append_result(A, attacker, ev)
        except Exception:
            pass
        self.assertEqual(c.unprocessed(A, victim), [ev])            # old: the victim's turn is silently lost

    def test_a_non_input_event_is_refused(self):
        s = c.sid()
        c.finish(A, s)
        fin = c.events(A, s, "session_finished")[0][1]
        with self.assertRaises(Exception):
            c.read_digest(A, s, fin)


class M3Rollup(unittest.TestCase):
    def test_rollup_identity_is_the_scheduled_deadline_not_a_counter(self):
        s = c.sid()
        c.rollup(A, s, 0, 1000000)       # a regressed counter reuses ordinal 0 for a LATER rollup
        c.rollup(A, s, 0, 2000000)
        self.assertEqual(len(c.events(A, s, "rollup")), 2)          # old: the second is silently deduplicated away


class M4Erased(unittest.TestCase):
    def test_erased_subject_raises_a_non_retryable_class(self):
        s = c.sid()
        ev = c.make_input(A, s, "r1")
        c.erase_subject(A, s)
        with self.assertRaises(Exception) as cm:
            c.read_digest(A, s, ev)
        self.assertEqual(type(cm.exception).__name__, "SubjectErased")   # old: LookupError, retried forever


class M5Idempotent(unittest.TestCase):
    def test_input_creation_is_idempotent_by_request_id(self):
        s = c.sid()
        e1 = c.make_input(A, s, "same-request")
        e2 = c.make_input(A, s, "same-request")                      # an API retry after a lost response
        self.assertEqual(e1, e2)
        self.assertEqual(len(c.events(A, s, "turn_input")), 1)


if __name__ == "__main__":
    unittest.main()
