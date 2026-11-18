"""Offline test of runkit/chain_singles.sh and runkit/launch_one.sh (review D F9): a failing cleanup stops the chain, and
the launcher records the experiment's exit status and the gate's verdict as separate fields. The real scripts of this
tree run with stubs on PATH for python (WP_HOME/venv/bin/python), df and setsid; nothing touches a container.
usage (from writepath/): python3 -m unittest tests.test_chain_singles -v"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]

# The stub interpreter: `-m exp.disk_gate <cmd>` exits with $GATE_<CMD> (default 0; `version` also prints the two
# fields the real gate prints), `-m exp.cleanup <x>` with $CLEANUP_RC, any other module with $EXP_RC.
PY = """#!/bin/sh
[ "$1" = "-m" ] || exit 9
m=$2; shift 2
case "$m" in
  exp.disk_gate) eval "rc=\\${GATE_$(echo "$1" | tr a-z- A-Z_):-0}"; echo "disk_gate $1 stub"
    [ "$1" = version ] && [ "$rc" = 0 ] && { echo "gate_version=disk_gate stub sha256 0"; echo "gate_limits=LIMITS stub"; }
    exit "$rc" ;;
  exp.cleanup) echo "cleanup $1 stub output"; exit "${CLEANUP_RC:-0}" ;;
  *) echo "module $m stub"; exit "${EXP_RC:-0}" ;;
esac
"""


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "venv" / "bin").mkdir(parents=True)
        (self.home / "results").mkdir()
        py = self.home / "venv" / "bin" / "python"
        py.write_text(PY)
        py.chmod(0o755)
        self.pinned = self.home / "runs" / "abc123-t"
        self.pinned.mkdir(parents=True)
        (self.pinned / "GIT_HEAD").write_text("abc123\n")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "df").write_text("#!/bin/sh\necho Avail\necho 31457280\n")        # 30 GiB in KiB
        (bindir / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
        for f in ("df", "setsid"):
            (bindir / f).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", WP_HOME=str(self.home))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chain(self, spec, **env):
        sp = self.tmp / "spec.txt"
        sp.write_text(spec)
        r = subprocess.run(["sh", str(HERE / "runkit" / "chain_singles.sh"), str(self.pinned), str(sp)],
                           capture_output=True, text=True, env=dict(self.env, **env), timeout=120)
        return r.returncode, r.stdout + r.stderr

    def out(self, label):
        return (self.home / "results" / f"one.abc123.{label}.out").read_text()

    def test_failed_cleanup_stops_the_chain(self):
        rc, out = self.chain("first exp.some_cleanup exp.first\nsecond - exp.second\n", CLEANUP_RC="4")
        self.assertEqual(rc, 3, f"a failed cleanup must stop the chain (exit 3); output tail: {out[-300:]}")
        self.assertFalse((self.home / "results" / "one.abc123.second.out").exists(),
                         "the chain ran the next experiment after a failed cleanup")

    def test_successful_cleanup_continues(self):
        rc, out = self.chain("first exp.some_cleanup exp.first\nsecond - exp.second\n")
        self.assertEqual(rc, 0, out[-300:])
        self.assertIn("gate=VALID", self.out("second"))

    def test_experiment_failure_and_gate_verdict_are_separate_fields(self):
        rc, _ = self.chain("first - exp.first\n", EXP_RC="1")
        o = self.out("first")
        self.assertIn("experiment_exit=1", o, "the experiment's exit status must be its own field")
        self.assertIn("gate=VALID", o, "a failing experiment under a valid gate is VALID for the gate")

    def test_gate_void_at_end_is_its_own_field(self):
        rc, _ = self.chain("first - exp.first\n", GATE_END="3")
        o = self.out("first")
        self.assertIn("experiment_exit=0", o)
        self.assertIn("gate=VOID", o)
        self.assertEqual(rc, 3, "the chain stops after a VOID run")

    def test_gate_blocked_at_start_runs_nothing(self):
        rc, _ = self.chain("first - exp.first\n", GATE_START="2")
        o = self.out("first")
        self.assertIn("gate=BLOCKED", o)
        self.assertIn("experiment_exit=not run", o)
        self.assertNotIn("module exp.first stub", o)

    def test_every_outcome_states_the_gate_version_and_limits(self):
        # review F F19 (GATE-DESIGN 6.2 B8): the run's output names the gate that judged it and the limits of its
        # guarantee, before the verdict, whatever the verdict is
        for i, env in enumerate(({}, {"GATE_START": "2"}, {"GATE_CHECK": "3"}, {"GATE_END": "3"})):
            label = f"run{i}"
            self.chain(f"{label} - exp.first\n", **env)
            o = self.out(label)
            for field in ("gate_version=disk_gate stub sha256 0", "gate_limits=LIMITS stub"):
                self.assertIn(field, o, f"{field.split('=')[0]} missing from the run's output under {env or 'VALID'}")
                self.assertLess(o.index(field), o.index("gate="), f"{field.split('=')[0]} must precede the verdict")

    def test_gate_that_cannot_name_itself_runs_nothing(self):
        self.chain("first - exp.first\n", GATE_VERSION="1")
        o = self.out("first")
        self.assertIn("gate=BLOCKED", o)
        self.assertNotIn("module exp.first stub", o)
        self.assertNotIn("disk_gate start stub", o)


if __name__ == "__main__":
    unittest.main(verbosity=2)
