"""Deploy part 1 (goodqa#16096): the durable, operational alert for a poisoned input, and no cap on how many a session may have.

Logic tests run with fakes (no Postgres); where psycopg or temporalio are absent (the Mac) minimal stand-ins are installed so
the REAL `activities.tracked` wrapper still runs. The Postgres-backed class runs only with ENGINE_TEST_PG=1 against a scratch
engine database (t2-dev, under the lab run lock).

PENDING_REAL_TEST D1-poison: (a) a poison raises a durable alert before the workflow hears of it, even when the dead-letter path
fails; (b) 25 poisoned inputs in one session (more than the old 20) do not hold back input 26, and none is re-queued."""
import contextlib
import io
import json
import os
import sys
import types
import unittest
import uuid
from unittest import mock


def _stub_if_missing():
    try:
        import psycopg, psycopg_pool, temporalio  # noqa: F401
        return False
    except ImportError:
        pass
    pg = types.ModuleType("psycopg")
    for n in ("OperationalError", "InterfaceError"):
        setattr(pg, n, type(n, (Exception,), {}))
    pg.errors = types.SimpleNamespace(**{n: type(n, (pg.OperationalError,), {}) for n in
                                         ("QueryCanceled", "DiskFull", "OutOfMemory", "ProgramLimitExceeded", "InsufficientResources")})
    pool = types.ModuleType("psycopg_pool")
    pool.PoolTimeout = type("PoolTimeout", (Exception,), {})
    pool.PoolClosed = type("PoolClosed", (Exception,), {})
    t = types.ModuleType("temporalio")
    t.activity = types.SimpleNamespace(defn=lambda fn: fn, info=lambda: (_ for _ in ()).throw(RuntimeError("outside")))
    ex = types.ModuleType("temporalio.exceptions")

    class ApplicationError(Exception):
        def __init__(self, message, type=None, non_retryable=False):
            super().__init__(message)
            self.type, self.non_retryable = type, non_retryable
    ex.ApplicationError = ApplicationError
    t.exceptions = ex
    sys.modules.update({"psycopg": pg, "psycopg_pool": pool, "temporalio": t, "temporalio.exceptions": ex})
    return True


STUBBED = _stub_if_missing()
from engine import activities, pgstore  # noqa: E402

TENANT, SID, EID = "t1", "s1", "t1/s1/2"


def call_poisoned(fn_name="turn_read", args=(TENANT, SID, EID, []), attempt=5, exc=RuntimeError("secret content here")):
    """Run the REAL tracked wrapper around a function that raises `exc`, with the activity attempt patched."""
    def body(*a):
        raise exc
    body.__name__ = fn_name
    wrapped = activities.tracked(body)
    with mock.patch.object(activities, "_attempt", lambda: attempt):
        return wrapped(*args)


class AlertFromTheWrapper(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.spool = os.path.join(tempfile.mkdtemp(), "spool.jsonl")
        self._env = mock.patch.dict(os.environ, {"ENGINE_ALERT_SPOOL": self.spool})
        self._env.start()
        self.addCleanup(self._env.stop)

    def run_case(self, **kw):
        calls, order = [], []
        fake = mock.Mock(side_effect=lambda *a: (calls.append(a), order.append("alert"), True)[2])
        err = io.StringIO()
        with mock.patch.object(pgstore, "raise_alert", fake, create=True), contextlib.redirect_stderr(err):
            try:
                call_poisoned(**kw)
                raised = None
            except Exception as e:  # noqa: BLE001
                order.append("raise")
                raised = e
        return calls, order, raised, err.getvalue()

    def test_a_poison_raises_a_durable_alert_before_the_non_retryable_failure(self):
        calls, order, raised, log = self.run_case()
        self.assertEqual(calls, [(TENANT, SID, EID, "poison", "RuntimeError", 5)])
        self.assertEqual(order, ["alert", "raise"])
        self.assertEqual((raised.type, raised.non_retryable), ("Poison", True))

    def test_the_log_line_carries_ids_and_the_class_never_the_exception_text(self):
        _, _, _, log = self.run_case(exc=RuntimeError("SENSITIVE-PAYLOAD-MARKER"))
        self.assertTrue(log.strip(), "witness: an alert line was logged")
        rec = json.loads(log.strip().splitlines()[-1])
        self.assertEqual((rec["engine_alert"], rec["event"], rec["class"]), ("poison", EID, "RuntimeError"))
        self.assertNotIn("SENSITIVE-PAYLOAD-MARKER", log)

    def test_a_retry_before_the_poison_threshold_raises_no_alert(self):
        calls, _, raised, _ = self.run_case(attempt=4)
        self.assertEqual(calls, [])
        self.assertIsInstance(raised, RuntimeError)              # retried, not poisoned

    def test_invalid_input_and_erased_subject_raise_no_alert(self):
        for exc in (pgstore.InvalidInput("x"), pgstore.SubjectErased("x")):
            calls, _, _, _ = self.run_case(exc=exc)
            self.assertEqual(calls, [], type(exc).__name__)

    def test_an_outage_is_retried_without_an_alert(self):
        calls, _, raised, _ = self.run_case(exc=activities.psycopg.OperationalError("down"), attempt=50)
        self.assertEqual(calls, [])
        self.assertIsInstance(raised, activities.psycopg.OperationalError)

    def test_a_repeat_does_not_log_again(self):
        fake = mock.Mock(return_value=False)                      # already existed
        err = io.StringIO()
        with mock.patch.object(pgstore, "raise_alert", fake, create=True), contextlib.redirect_stderr(err):
            with self.assertRaises(Exception):
                call_poisoned()
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(err.getvalue(), "")

    def test_a_non_turn_session_activity_poison_is_attached_to_the_session_not_an_event(self):
        calls, _, _, _ = self.run_case(fn_name="rollup", args=(TENANT, SID, 123))
        self.assertEqual(calls, [(TENANT, SID, "activity:rollup", "poison", "RuntimeError", 5)])

    def test_an_activity_with_no_session_alerts_under_the_unattached_key_and_fails_loudly(self):
        calls, _, raised, log = self.run_case(fn_name="get_config", args=("target_queue",))
        self.assertEqual(calls, [("-", "-", "activity:get_config", "poison", "RuntimeError", 5)])
        self.assertEqual(raised.type, "Poison")
        self.assertEqual(json.loads(log.strip().splitlines()[-1])["engine_alert"], "poison_unattached")

    # ---- F2: a persistent alert-write failure is bounded and has a defined fail-safe
    def failing_writes(self, attempt):
        err = io.StringIO()
        boom = mock.Mock(side_effect=activities.psycopg.OperationalError("alert table not writable"))
        with mock.patch.object(pgstore, "raise_alert", boom, create=True), contextlib.redirect_stderr(err):
            try:
                call_poisoned(attempt=attempt)
                got = None
            except Exception as e:  # noqa: BLE001
                got = e
        return got, err.getvalue()

    def test_a_failing_alert_write_is_retried_with_the_activity_for_a_bounded_number_of_attempts(self):
        for attempt in range(5, 10):
            got, _ = self.failing_writes(attempt)
            self.assertIsInstance(got, activities.psycopg.OperationalError, "attempt %d retries" % attempt)

    def test_after_the_bound_the_alert_is_spooled_loudly_and_poison_goes_through(self):
        got, log = self.failing_writes(10)
        self.assertEqual((getattr(got, "type", None), getattr(got, "non_retryable", None)), ("Poison", True),
                         "persistent alert failure must not retry for ever: got %r" % (got,))
        rec = json.loads(log.strip().splitlines()[-1])
        self.assertEqual((rec["engine_alert"], rec["write_error"], rec["event"]), ("poison_alert_spooled", "OperationalError", EID))
        with open(self.spool) as fh:
            spooled = [json.loads(x) for x in fh]
        self.assertEqual([(r["tenant"], r["session"], r["event"], r["class"]) for r in spooled], [(TENANT, SID, EID, "RuntimeError")])
        self.assertEqual(oct(os.stat(self.spool).st_mode & 0o777), "0o600")
        self.assertNotIn("SENSITIVE", json.dumps(spooled))

    def test_the_spool_drains_into_the_alert_table_and_keeps_what_fails(self):
        from engine import alertspool
        for i in range(3):
            alertspool.append({"tenant": TENANT, "session": SID, "event": "%s/%d" % ("t1/s1", i), "class": "RuntimeError", "attempt": 10})
        seen = []

        def write(rec):
            if rec["event"].endswith("/1"):
                raise RuntimeError("still failing")
            seen.append(rec["event"])
        self.assertEqual(alertspool.drain(write), {"written": 2, "kept": 1})
        self.assertEqual(seen, ["t1/s1/0", "t1/s1/2"])
        self.assertEqual(alertspool.drain(lambda r: None), {"written": 1, "kept": 0})
        self.assertEqual(alertspool.drain(lambda r: None), {"written": 0, "kept": 0})
        self.assertFalse(os.path.exists(self.spool))


class ResubmissionPlan(unittest.TestCase):
    def test_a_resubmission_records_its_origin_and_depth(self):
        new = pgstore.plan_resubmission({"n": 1, "text": "x"}, "t1/s1/2")
        self.assertEqual(new, {"n": 1, "text": "x", "retry_of": "t1/s1/2", "retry_depth": 1})

    def test_the_chain_is_bounded_so_a_still_poisoned_input_cannot_loop(self):
        content = {"n": 1}
        for expected in (1, 2, 3):
            content = pgstore.plan_resubmission(content, "t1/s1/%d" % expected)
            self.assertEqual(content["retry_depth"], expected)
        with self.assertRaises(pgstore.RetryRefused):
            pgstore.plan_resubmission(content, "t1/s1/9")

    def test_malformed_content_or_depth_is_refused(self):
        for bad in ("text", ["a"], None, {"retry_depth": "1"}, {"retry_depth": True}, {"retry_depth": -1}):
            with self.assertRaises(pgstore.RetryRefused, msg=repr(bad)):
                pgstore.plan_resubmission(bad, "t1/s1/2")


class SqlCarriesTheSkip(unittest.TestCase):
    def test_both_queries_exclude_alerted_inputs_in_sql(self):
        for fn in (pgstore.has_older_unprocessed, pgstore.unprocessed_inputs):
            seen = []

            class Cur:
                def execute(self, q, a=()):
                    seen.append(q)
                    return self

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            class Conn(Cur):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            with mock.patch.object(pgstore, "pool", lambda: types.SimpleNamespace(connection=lambda: Conn())):
                if fn is pgstore.has_older_unprocessed:
                    fn(TENANT, SID, "t1/s1/9", skip=())
                else:
                    fn(TENANT, SID)
            self.assertIn("engine_alerts", seen[0], fn.__name__)

    def test_schema_file_defines_the_table_and_the_statuses(self):
        here = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(here, "engine_schema.sql")) as fh:
            ddl = fh.read()
        self.assertIn("create table if not exists engine_alerts", ddl)
        self.assertIn("check (status in ('open','acked','released'))", ddl)
        self.assertIn("retried_as", ddl)


@unittest.skipUnless(os.environ.get("ENGINE_TEST_PG") == "1", "needs a scratch engine Postgres: ENGINE_TEST_PG=1 (t2-dev, run lock)")
class PostgresBacked(unittest.TestCase):
    """Real Postgres, the real activity wrapper and the real store functions. The poison outcomes are PRODUCED (the engine's own test
    knob ENGINE_POISON_SEQS makes read_digest fail deterministically), never hand-made alert rows."""
    N_POISON = 25

    def setUp(self):
        pgstore.ensure_alert_schema()
        self.sid = "pa" + uuid.uuid4().hex[:8]

    def inputs(self, n):
        return [pgstore.append_input(TENANT, self.sid, "p%d" % i, {"n": i})[0] for i in range(n)]

    def poison_through_the_wrapper(self, eid, dead_letter_works):
        """Drive turn_read to its real poison outcome (attempt 5), then the workflow's reaction: dead_letter."""
        seq = str(pgstore.keys.parse_event_id(eid)[2])
        with mock.patch.dict(os.environ, {"ENGINE_POISON_SEQS": seq}), mock.patch.object(activities, "_attempt", lambda: 5):
            with self.assertRaises(Exception) as cm:
                activities.turn_read(TENANT, self.sid, eid, [])
            self.assertEqual(getattr(cm.exception, "type", None), "Poison")
        env = {} if dead_letter_works else {"ENGINE_FAIL_DEAD_LETTER": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(activities, "_attempt", lambda: 5):
            try:
                activities.dead_letter(TENANT, self.sid, eid, "Poison")
                return "dead"
            except Exception as e:  # noqa: BLE001
                self.assertEqual(getattr(e, "type", None), "Poison")
                return "stuck"

    def process_like_the_workflow(self, eid):
        digest = activities.turn_read(TENANT, self.sid, eid, [])
        key = activities.turn_effect(TENANT, self.sid, eid)
        return activities.turn_append(TENANT, self.sid, eid, digest, key, "v1")

    def test_more_than_twenty_poisoned_inputs_do_not_block_the_next_one_and_are_not_requeued(self):
        poison = self.inputs(self.N_POISON)
        good = pgstore.append_input(TENANT, self.sid, "good", {"n": 999})[0]
        self.assertTrue(pgstore.has_older_unprocessed(TENANT, self.sid, good, skip=poison[:20]),
                        "witness (the reviewed 20-item skip list): input 21 still blocks input 26")
        for eid in poison:
            self.poison_through_the_wrapper(eid, dead_letter_works=False)
        self.assertFalse(pgstore.has_older_unprocessed(TENANT, self.sid, good, skip=[]))
        self.assertEqual(pgstore.unprocessed_inputs(TENANT, self.sid, limit=100), [good])
        self.assertEqual(len([r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]), self.N_POISON)

    def test_alert_is_idempotent_per_input_and_survives_a_fresh_pool(self):
        eid = self.inputs(1)[0]
        self.assertTrue(pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError", 5))
        self.assertFalse(pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError", 6))     # crash-retry
        self.assertFalse(pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError", 5))
        rows = [r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]
        self.assertEqual((len(rows), rows[0][5]), (1, 6), "one row; last_attempt is the highest seen, not a count")
        pgstore._pool = None
        self.assertEqual(len([r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]), 1)

    def release_case(self, dead_letter_works):
        poisoned, later = self.inputs(2)
        self.assertEqual(self.poison_through_the_wrapper(poisoned, dead_letter_works), "dead" if dead_letter_works else "stuck")
        self.assertNotIn(poisoned, pgstore.unprocessed_inputs(TENANT, self.sid, limit=100), "witness: skipped after the real poison")
        # the cause is fixed: the knob is off for the retry. The new input is a different seq, so it is not poisoned.
        new = pgstore.release_alert(TENANT, self.sid, poisoned, "poison", "oncall")
        self.assertNotEqual(new, poisoned)
        self.assertEqual(pgstore.release_alert(TENANT, self.sid, poisoned, "poison", "oncall"), new, "idempotent")
        pending = pgstore.unprocessed_inputs(TENANT, self.sid, limit=100)
        self.assertEqual(pending, [later, new], "the original stays skipped; the retry goes to the end of the sequence")
        for eid in pending:
            counted = self.process_like_the_workflow(eid)
            self.assertTrue(counted[1])
        self.assertEqual(pgstore.unprocessed_inputs(TENANT, self.sid, limit=100), [])
        self.assertTrue(pgstore.verify_session_chain(TENANT, self.sid)[0], "the hash chain is intact (nothing was deleted or rewritten)")
        released = [r for r in pgstore.list_alerts(TENANT, "released", 1000) if r[1] == self.sid]
        self.assertEqual([(r[2], r[9]) for r in released], [(poisoned, new)])

    def test_release_after_a_successful_dead_letter_retries_the_input(self):
        self.release_case(dead_letter_works=True)

    def test_release_after_a_failed_dead_letter_retries_the_input(self):
        self.release_case(dead_letter_works=False)

    def test_a_resubmission_that_poisons_again_cannot_loop(self):
        eid = self.inputs(1)[0]
        for depth in range(3):
            self.poison_through_the_wrapper(eid, dead_letter_works=True)
            eid = pgstore.release_alert(TENANT, self.sid, eid, "poison", "oncall")
            # poison again: make the new input's seq the poisoned one
            self.poison_through_the_wrapper(eid, dead_letter_works=True)
        with self.assertRaises(pgstore.RetryRefused):
            pgstore.release_alert(TENANT, self.sid, eid, "poison", "oncall")

    def test_release_refuses_unknown_alerts_and_other_tenants(self):
        eid = self.inputs(1)[0]
        with self.assertRaises(pgstore.RetryRefused):
            pgstore.release_alert(TENANT, self.sid, eid, "poison", "oncall")          # no alert yet
        self.poison_through_the_wrapper(eid, True)
        with self.assertRaises(pgstore.RetryRefused):
            pgstore.release_alert("t2", self.sid, eid, "poison", "oncall")            # event id of another tenant

    def test_ack_keeps_the_input_skipped(self):
        eid = self.inputs(1)[0]
        self.poison_through_the_wrapper(eid, False)
        self.assertTrue(pgstore.ack_alert(TENANT, self.sid, eid, "poison", "oncall"))
        self.assertFalse(pgstore.ack_alert(TENANT, self.sid, eid, "poison", "oncall"))
        self.assertNotIn(eid, pgstore.unprocessed_inputs(TENANT, self.sid, limit=100))

    def test_one_tenants_alert_does_not_hide_another_tenants_input(self):
        eid2 = pgstore.append_input("t2", self.sid, "x", {"n": 1})[0]
        eid1 = self.inputs(1)[0]
        pgstore.raise_alert(TENANT, self.sid, eid1, "poison", "RuntimeError", 5)
        self.assertIn(eid2, pgstore.unprocessed_inputs("t2", self.sid, limit=100))

    def test_unattached_alert_is_stored(self):
        self.assertIn(pgstore.raise_alert("-", "-", "activity:get_config", "poison", "RuntimeError", 5), (True, False))
        self.assertTrue([r for r in pgstore.list_alerts("-", "open", 1000) if r[2] == "activity:get_config"])


if __name__ == "__main__":
    unittest.main()
