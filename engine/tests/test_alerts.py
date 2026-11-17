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
        self.assertEqual(calls, [(TENANT, SID, EID, "poison", "RuntimeError")])
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

    def test_if_the_alert_cannot_be_written_the_activity_retries_instead_of_going_poison_without_a_record(self):
        with mock.patch.object(pgstore, "raise_alert", mock.Mock(side_effect=activities.psycopg.OperationalError("pg down")), create=True):
            try:
                call_poisoned()
                got = None
            except Exception as e:  # noqa: BLE001
                got = e
            self.assertIsInstance(got, activities.psycopg.OperationalError, "the alert error propagates (retry), not Poison: %r" % (got,))

    def test_a_repeat_does_not_log_again(self):
        fake = mock.Mock(return_value=False)                      # already existed
        err = io.StringIO()
        with mock.patch.object(pgstore, "raise_alert", fake, create=True), contextlib.redirect_stderr(err):
            with self.assertRaises(Exception):
                call_poisoned()
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(err.getvalue(), "")

    def test_a_non_turn_activity_poison_is_attached_to_the_session_not_an_event(self):
        calls, _, _, _ = self.run_case(fn_name="rollup", args=(TENANT, SID, 123))
        self.assertEqual(calls, [(TENANT, SID, "activity:rollup", "poison", "RuntimeError")])
        calls, _, raised, _ = self.run_case(fn_name="get_config", args=("target_queue",))
        self.assertEqual(calls, [], "no session to attach to")
        self.assertEqual(raised.type, "Poison")


class SqlCarriesTheSkip(unittest.TestCase):
    def test_both_queries_exclude_open_and_acked_alerts_in_sql(self):
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
            self.assertIn("'open','acked'", seen[0], fn.__name__)
        self.assertNotIn("released", pgstore.ALERT_SKIP)

    def test_schema_file_defines_the_table_and_the_statuses(self):
        here = os.path.dirname(os.path.dirname(__file__))
        ddl = open(os.path.join(here, "engine_schema.sql")).read()
        self.assertIn("create table if not exists engine_alerts", ddl)
        self.assertIn("check (status in ('open','acked','released'))", ddl)


@unittest.skipUnless(os.environ.get("ENGINE_TEST_PG") == "1", "needs a scratch engine Postgres: ENGINE_TEST_PG=1 (t2-dev, run lock)")
class PostgresBacked(unittest.TestCase):
    """25 poisoned inputs in ONE session (the old skip list held 20), then one good input."""
    N_POISON = 25

    def setUp(self):
        pgstore.ensure_alert_schema()
        self.sid = "pa" + uuid.uuid4().hex[:8]
        self.poison = [pgstore.append_input(TENANT, self.sid, "p%d" % i, {"n": i})[0] for i in range(self.N_POISON)]
        self.good = pgstore.append_input(TENANT, self.sid, "good", {"n": 999})[0]

    def test_poisoned_inputs_beyond_twenty_do_not_block_the_next_one_and_are_not_requeued(self):
        # witness: before any alert, every input is unprocessed and the good one waits behind the poisoned ones
        self.assertTrue(pgstore.has_older_unprocessed(TENANT, self.sid, self.good, skip=self.poison[:20]),
                        "prior behaviour: a skip list capped at 20 leaves input 21 to block input 26")
        for eid in self.poison:
            self.assertTrue(pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError"))
        self.assertFalse(pgstore.has_older_unprocessed(TENANT, self.sid, self.good, skip=[]), "no carried list needed")
        self.assertEqual(pgstore.unprocessed_inputs(TENANT, self.sid, limit=100), [self.good], "alerted inputs are never re-queued")
        self.assertEqual(len([r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]), self.N_POISON)

    def test_alert_is_idempotent_and_survives_a_fresh_connection_pool(self):
        self.assertTrue(pgstore.raise_alert(TENANT, self.sid, self.poison[0], "poison", "RuntimeError"))
        self.assertFalse(pgstore.raise_alert(TENANT, self.sid, self.poison[0], "poison", "RuntimeError"))
        rows = [r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]
        self.assertEqual((len(rows), rows[0][5]), (1, 2))
        pgstore._pool = None                                      # a restarted worker has a new pool; the row is in Postgres
        self.assertEqual(len([r for r in pgstore.list_alerts(TENANT, "open", 1000) if r[1] == self.sid]), 1)

    def test_ack_keeps_the_skip_and_release_lifts_it_and_a_new_poison_reopens(self):
        eid = self.poison[0]
        pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError")
        self.assertTrue(pgstore.set_alert_status(TENANT, self.sid, eid, "poison", "acked", "oncall"))
        self.assertNotIn(eid, pgstore.unprocessed_inputs(TENANT, self.sid, limit=100))
        pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError")      # a repeat does not reopen an acked alert
        self.assertEqual([r[6] for r in pgstore.list_alerts(TENANT, "acked", 1000) if r[2] == eid], ["acked"])
        self.assertTrue(pgstore.set_alert_status(TENANT, self.sid, eid, "poison", "released", "oncall"))
        self.assertIn(eid, pgstore.unprocessed_inputs(TENANT, self.sid, limit=100))   # retried
        pgstore.raise_alert(TENANT, self.sid, eid, "poison", "RuntimeError")      # and it failed again: reopened
        self.assertEqual([r[6] for r in pgstore.list_alerts(TENANT, "open", 1000) if r[2] == eid], ["open"])

    def test_one_tenants_alert_does_not_hide_another_tenants_input(self):
        other = "t2"
        eid2 = pgstore.append_input(other, self.sid, "x", {"n": 1})[0]
        pgstore.raise_alert(TENANT, self.sid, self.poison[0], "poison", "RuntimeError")
        self.assertIn(eid2, pgstore.unprocessed_inputs(other, self.sid, limit=100))


if __name__ == "__main__":
    unittest.main()
