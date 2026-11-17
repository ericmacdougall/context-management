"""Deploy part 2 (goodqa#16096): downgrade guards. Pure unit tests; the interceptor test needs the temporalio package and is
skipped without it (run it with the engine venv on t2-dev). No Postgres, no Temporal server.

PENDING_REAL_TEST D2-downgrade: an older worker and an older server image are each refused against newer data."""
import contextlib
import glob
import importlib
import io
import json
import os
import re
import unittest

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
        for bad in ("latest", "vv1.2", "1.-2", "-1.2", "+1.2", "1..2", "", "1.2.", "v", "1.2a"):
            with self.assertRaises(g.InvalidVersion, msg=repr(bad)):
                g.check_schema({"main": "1.18", "visibility": "1.13"}, {"main": bad, "visibility": "1.13"})
        g.check_schema({"main": "v1.18", "visibility": "1.13"}, {"main": "1.18", "visibility": "v1.13"})   # one optional v

    def test_cli_fails_closed_on_a_malformed_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = g.main(["schema", "--expected-main", "1.18", "--expected-visibility", "1.13", "--actual-main", "garbage", "--actual-visibility", "1.13"])
        self.assertEqual((rc, json.loads(out.getvalue())["refused"]), (3, "InvalidVersion"))

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
    def test_a_newer_state_is_refused_by_name(self):
        for ver in (2, 3, 99):
            with self.assertRaises(g.StateTooNew) as cm:
                g.check_state({"ver": ver}, 1)
            self.assertIn("roll forward", str(cm.exception))

    def test_equal_older_and_absent_versions_run(self):
        self.assertEqual(g.check_state({"ver": 1}, 1), 1)
        self.assertEqual(g.check_state({"ver": 1}, 2), 1)
        self.assertEqual(g.check_state({"ver": 2}, 2), 2)
        self.assertEqual(g.check_state({"tenant": "t1"}, 1), 1)           # no ver: version 1 (explicit legacy policy)

    def test_an_unusable_version_or_a_non_object_is_refused_not_ignored(self):
        for bad in ("2", 2.0, None, True, False, [2], 0, -1, -99):
            with self.assertRaises(g.InvalidState, msg=repr(bad)):
                g.check_state({"ver": bad}, 5)
        for bad in (None, [], "state", 7):
            with self.assertRaises(g.InvalidState, msg=repr(bad)):
                g.check_state(bad, 5)

    def test_state_module_supports_what_the_guard_checks(self):
        v1 = state.new_state("t1", "s1", ver=1)
        self.assertEqual(g.check_state(v1, 1), 1)
        self.assertEqual(g.check_state(state.migrate(v1), state.CURRENT), state.CURRENT)
        with self.assertRaises(g.StateTooNew):
            g.check_state(state.migrate(v1), 1)                           # v1 worker, v2 state


WF = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wf")
VARIANTS = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(WF, "v*.py")))


class GuardIsInEveryWorkflowInit(unittest.TestCase):
    """Source-level (runs without temporalio): the guard is the FIRST statement of __init__ in every workflow variant, and the
    supported version is 2 only where the state is migrated (v2s, v2sx)."""

    def test_every_variant_guards_first_with_the_right_supported_version(self):
        self.assertGreaterEqual(len(VARIANTS), 12)
        for v in VARIANTS:
            with open(os.path.join(WF, v + ".py")) as fh:
                src = fh.read()
            m = re.search(r"def __init__\(self, st: dict\):\n(\s+)(.+)\n", src)
            self.assertIsNotNone(m, v)
            self.assertIn("guard_mod.check_state(st, SUPPORTED_STATE_VER)", m.group(2), "%s: guard is not the first statement" % v)
            want = "2" if v in ("v2s", "v2sx") else "1"
            self.assertRegex(src, r"SUPPORTED_STATE_VER = %s\b" % want, v)

    def test_variants_are_what_gen_variants_generates(self):
        """A regeneration must not change any committed variant (so the guard cannot silently drift from v1)."""
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        before = {v: open(os.path.join(WF, v + ".py")).read() for v in VARIANTS}
        subprocess.run([sys.executable, "-m", "engine.gen_variants"], cwd=root, check=True, capture_output=True)
        after = {v: open(os.path.join(WF, v + ".py")).read() for v in VARIANTS}
        self.assertEqual(before, after)


class _Spy(dict):
    """A state that records every access except the version lookup the guard is allowed to make."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.touched = []

    def get(self, key, default=None):
        if key != "ver":
            self.touched.append(("get", key))
        return super().get(key, default)

    def __getitem__(self, key):
        self.touched.append(("getitem", key))
        return super().__getitem__(key)

    def __iter__(self):
        self.touched.append("iter")
        return super().__iter__()

    def keys(self):
        self.touched.append("keys")
        return super().keys()


@unittest.skipUnless(HAVE_TEMPORAL, "needs temporalio (engine venv on t2-dev)")
class WorkflowInitRefuses(unittest.TestCase):
    """The REAL workflow classes, constructed the way the SDK constructs them (workflow init). On the base (no guard) the
    newer-state cases do not raise StateTooNew: v1 code reads the state and runs."""

    def build(self, variant, state_in):
        mod = importlib.import_module("engine.wf." + variant)
        return mod, lambda: mod.SessionWorkflow(state_in)

    def test_every_variant_refuses_a_newer_state_before_touching_it(self):
        for v in VARIANTS:
            mod = importlib.import_module("engine.wf." + v)
            spy = _Spy(state.new_state("t1", "s1", ver=1))
            spy["ver"] = mod.SUPPORTED_STATE_VER + 1
            spy.touched.clear()
            with self.assertRaises(g.StateTooNew, msg=v):
                mod.SessionWorkflow(spy)
            self.assertEqual(spy.touched, [], "%s read the state before refusing: %r" % (v, spy.touched))

    def test_every_variant_still_accepts_its_own_version(self):
        for v in VARIANTS:
            mod = importlib.import_module("engine.wf." + v)
            st = state.new_state("t1", "s1", ver=mod.SUPPORTED_STATE_VER)
            wf = mod.SessionWorkflow(st)
            self.assertEqual(wf.st["tenant"], "t1", v)

    def test_a_v1_worker_refuses_a_migrated_state_and_the_v2_worker_runs_it(self):
        v2 = state.migrate(state.new_state("t1", "s1", ver=1, rollups=3))
        with self.assertRaises(g.StateTooNew):
            importlib.import_module("engine.wf.v1").SessionWorkflow(v2)
        wf = importlib.import_module("engine.wf.v2s").SessionWorkflow(v2)
        self.assertEqual(wf.st["rollup_count"], 3)

    def test_the_refusal_is_not_an_application_error(self):
        """An ApplicationError would FAIL the workflow (session over); a plain RuntimeError fails only the task, which retries."""
        from temporalio.exceptions import FailureError
        for cls in (g.StateTooNew, g.InvalidState):
            self.assertTrue(issubclass(cls, RuntimeError))
            self.assertFalse(issubclass(cls, FailureError))


if __name__ == "__main__":
    unittest.main()
