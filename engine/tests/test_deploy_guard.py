"""Deploy part 2 (goodqa#16096): downgrade guards. Pure unit tests; the interceptor test needs the temporalio package and is
skipped without it (run it with the engine venv on t2-dev). No Postgres, no Temporal server.

PENDING_REAL_TEST D2-downgrade: an older worker and an older server image are each refused against newer data."""
import asyncio
import io
import contextlib
import json
import unittest
from types import SimpleNamespace

from engine import deploy_guard as g
from engine import state

try:
    import temporalio  # noqa: F401
    HAVE_TEMPORAL = True
except ImportError:
    HAVE_TEMPORAL = False


class Schema(unittest.TestCase):
    def test_older_image_on_a_newer_schema_is_refused_for_either_database(self):
        with self.assertRaises(g.SchemaTooNew) as cm:
            g.check_schema({"main": "1.18", "visibility": "1.13"}, {"main": "1.19", "visibility": "1.13"})
        self.assertIn("restore the pre-upgrade snapshot", str(cm.exception))
        with self.assertRaises(g.SchemaTooNew):
            g.check_schema({"main": "1.18", "visibility": "1.13"}, {"main": "1.18", "visibility": "1.14"})

    def test_same_or_older_schema_is_the_normal_upgrade_path(self):
        self.assertEqual(g.check_schema({"main": "1.19", "visibility": "1.14"}, {"main": "1.18", "visibility": "1.13"})["actual"]["main"], "1.18")
        g.check_schema({"main": "1.19", "visibility": "1.14"}, {"main": "1.19", "visibility": "1.14"})

    def test_versions_compare_numerically_not_as_text(self):
        with self.assertRaises(g.SchemaTooNew):
            g.check_schema({"main": "1.9", "visibility": "1.9"}, {"main": "1.10", "visibility": "1.9"})   # "1.10" < "1.9" as text
        g.check_schema({"main": "1.10", "visibility": "1.9"}, {"main": "1.9", "visibility": "1.9"})

    def test_missing_or_malformed_input_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            g.check_schema({"main": "1.18"}, {"main": "1.18", "visibility": "1.13"})
        with self.assertRaises(ValueError):
            g.check_schema({"main": "1.18", "visibility": "1.13"}, {"main": "latest", "visibility": "1.13"})

    def test_cli_exit_code_and_message(self):
        argv = ["schema", "--expected-main", "1.18", "--expected-visibility", "1.13", "--actual-main", "1.19", "--actual-visibility", "1.14"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = g.main(argv)
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out.getvalue())["refused"], "SchemaTooNew")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = g.main(["schema", "--expected-main", "1.19", "--expected-visibility", "1.14", "--actual-main", "1.19", "--actual-visibility", "1.14"])
        self.assertEqual((rc, json.loads(out.getvalue())["ok"]), (0, True))


class WorkerState(unittest.TestCase):
    def test_prior_behaviour_an_older_worker_on_a_newer_state_fails_by_accident_or_not_at_all(self):
        v2 = state.migrate(state.new_state("t1", "s1", ver=1, rollups=3))
        with self.assertRaises(KeyError):
            v2["rollups"]                                    # F6: the v1 code reads st["rollups"] and the session sticks on a KeyError
        future = dict(state.new_state("t1", "s1", ver=1, rollups=3), ver=3, brand_new_field="x")   # a later additive version
        self.assertEqual(future["rollups"], 3)               # v1 code runs happily on it and would write back what it does not understand

    def test_a_newer_state_is_refused_by_name(self):
        for ver in (2, 3, 99):
            with self.assertRaises(g.StateTooNew) as cm:
                g.check_state({"ver": ver}, 1)
            self.assertIn("roll forward", str(cm.exception))

    def test_equal_older_and_absent_versions_run(self):
        self.assertEqual(g.check_state({"ver": 1}, 1), 1)
        self.assertEqual(g.check_state({"ver": 1}, 2), 1)
        self.assertEqual(g.check_state({"ver": 2}, 2), 2)
        self.assertEqual(g.check_state({"tenant": "t1"}, 1), 1)           # no ver: version 1
        self.assertEqual(g.check_state(None, 1), 1)

    def test_a_non_integer_version_is_refused_not_ignored(self):
        for bad in ("2", 2.0, None, True, [2]):
            with self.assertRaises(g.StateTooNew):
                g.check_state({"ver": bad}, 5)

    def test_state_module_supports_what_the_guard_checks(self):
        v1 = state.new_state("t1", "s1", ver=1)
        self.assertEqual(g.check_state(v1, 1), 1)
        self.assertEqual(g.check_state(state.migrate(v1), state.CURRENT), state.CURRENT)
        with self.assertRaises(g.StateTooNew):
            g.check_state(state.migrate(v1), 1)                           # v1 worker, v2 state


@unittest.skipUnless(HAVE_TEMPORAL, "needs temporalio (engine venv on t2-dev)")
class Interceptor(unittest.TestCase):
    def guard(self, supported):
        from engine.guard_interceptor import StateGuardInterceptor
        calls = []

        class Next:
            async def execute_workflow(self, input):
                calls.append(input)
                return "ran"
        cls = StateGuardInterceptor(supported).workflow_interceptor_class(None)
        return cls(Next()), calls

    def run_it(self, guard, state_in):
        return asyncio.new_event_loop().run_until_complete(guard.execute_workflow(SimpleNamespace(args=[state_in])))

    def test_older_worker_refuses_before_any_workflow_code_runs(self):
        guard, calls = self.guard(1)
        with self.assertRaises(g.StateTooNew):
            self.run_it(guard, {"ver": 2})
        self.assertEqual(calls, [], "the workflow was never started")

    def test_a_supporting_worker_runs_it(self):
        guard, calls = self.guard(2)
        self.assertEqual(self.run_it(guard, {"ver": 2}), "ran")
        self.assertEqual(len(calls), 1)

    def test_the_refusal_is_not_an_application_error(self):
        """An ApplicationError would FAIL the workflow (session over); a plain RuntimeError fails only the task, which retries."""
        from temporalio.exceptions import FailureError
        self.assertTrue(issubclass(g.StateTooNew, RuntimeError))
        self.assertFalse(issubclass(g.StateTooNew, FailureError))


if __name__ == "__main__":
    unittest.main()
