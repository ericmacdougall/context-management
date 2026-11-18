"""Offline test of runkit/chain_singles.sh (review D F9; review a108fc3 G-3, m-5, m-6; review aacf3c0 F-1, m-D, m-G): the
chain decides on the gate's exit status only, never on text the experiment can print; every attempt's record is kept;
it changes no store itself (the gate resets under its lock); a chain that ran nothing never exits 0; every step runs
isolated. The real scripts of this tree run with a stub interpreter on PATH (WP_HOME/venv/bin/python) and
stubs for df and setsid; nothing touches a container. The gate's own behaviour is tested in tests/test_disk_gate.py.
usage (from writepath/): python3 -m unittest tests.test_chain_singles -v"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]

# The stub interpreter. Every call is appended to $CALLS. `-m exp.disk_gate run LABEL MODULE ...` (gate v5) prints
# $EXP_PRINT (the worst case: experiment text reaching the gate's stream) and exits $GATE_RUN; the older launcher's
# `-m exp.disk_gate <cmd>` exits $GATE_<CMD> (`version` prints its two fields, `end` the attempt); `-m exp.cleanup`
# exits $CLEANUP_RC, `-m wp.setup` $RESET_RC, any other module (an experiment under the older launcher) prints
# $EXP_PRINT and exits $EXP_RC. $ATTEMPT marks which attempt printed a line.
# v6 (review aacf3c0 m-G): the chain starts `python -I -B <tree>/runkit/isolated.py MODULE ...`; the stub also accepts
# the older `-B -m MODULE` so that the older chain runs these tests to a FAIL, and records which form ran in $CALLS.form.
PY = """#!/bin/sh
f=
[ "$1" = "-I" ] && { f=I; shift; }
[ "$1" = "-B" ] && shift
case "$1" in
  -m) m=$2; f=m$f; shift 2 ;;
  */runkit/isolated.py) [ "$f" = I ] || exit 9; m=$2; f="iso:$1"; shift 2 ;;
  *) exit 9 ;;
esac
echo "$m $*" >> "$CALLS"
echo "$f $m" >> "$CALLS.form"
case "$m" in
  exp.disk_gate)
    if [ "$1" = run ]; then
      echo "disk_gate run $2 stub attempt=${ATTEMPT:-}"; printf '%b\\n' "${EXP_PRINT:-}"
      echo "disk_gate verdict exit ${GATE_RUN:-0} attempt=${ATTEMPT:-}"; exit "${GATE_RUN:-0}"
    fi
    eval "rc=\\${GATE_$(echo "$1" | tr a-z- A-Z_):-0}"; echo "disk_gate $1 stub"
    [ "$1" = version ] && [ "$rc" = 0 ] && { echo "gate_version=disk_gate stub sha256 0"; echo "gate_limits=LIMITS stub"; }
    [ "$1" = end ] && echo "attempt=${ATTEMPT:-}"
    exit "$rc" ;;
  exp.cleanup) echo "cleanup $1 stub output"; exit "${CLEANUP_RC:-0}" ;;
  wp.setup) echo "setup stub"; exit "${RESET_RC:-0}" ;;
  *) echo "module $m stub attempt=${ATTEMPT:-}"; printf '%b\\n' "${EXP_PRINT:-}"; exit "${EXP_RC:-0}" ;;
esac
"""


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "venv" / "bin").mkdir(parents=True)
        (self.home / "results").mkdir()
        (self.home / "state").mkdir()
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
        self.calls = self.tmp / "calls"
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", WP_HOME=str(self.home),
                        CALLS=str(self.calls))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chain(self, spec, timeout=120, **env):
        """(exit status, output); exit status None when the chain did not finish in time (review a108fc3 m-6)."""
        sp = self.tmp / "spec.txt"
        sp.write_text(spec)
        p = subprocess.Popen(["sh", str(HERE / "runkit" / "chain_singles.sh"), str(self.pinned), str(sp)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=dict(self.env, **env),
                             start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            return p.returncode, out
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, 9)                            # the test's own chain and its children only
            out, _ = p.communicate()
            return None, out + f"\n(chain did not finish within {timeout} s)"

    def ran(self, module):
        """Whether an experiment module was started: by the gate (v5: `exp.disk_gate run LABEL MODULE`) or directly
        by the older launcher (`MODULE ...`)."""
        for ln in (self.calls.read_text().splitlines() if self.calls.exists() else []):
            w = ln.split()
            if w[:1] == [module] or (w[:2] == ["exp.disk_gate", "run"] and w[3:4] == [module]):
                return True
        return False

    def files_with(self, text):
        return [p for p in self.home.rglob("*") if p.is_file() and text in p.read_text(errors="replace")]

    # ---- v6 (review aacf3c0 F-1): the chain changes no store; the gate resets and cleans under its run lock (a
    # failed reset is the gate's BLOCKED, test_gate_blocked_runs_nothing). The two v5 tests of a failed cleanup and a
    # failed reset in the chain (test_failed_cleanup_stops_the_chain, test_failed_store_reset_stops_the_chain) are
    # retired with that design: tests/test_disk_gate.py test_v6_f1_failed_reset_blocks covers it in the gate.
    def test_f1_chain_runs_no_store_tool_outside_the_gate(self):
        rc, out = self.chain("first exp.some_cleanup exp.first\nsecond - exp.second\n")
        self.assertEqual(rc, 0, out[-300:])
        mods = [ln.split()[0] for ln in self.calls.read_text().splitlines()]
        self.assertNotIn("exp.cleanup", mods, "the chain deleted indices outside a run's lock")
        self.assertNotIn("wp.setup", mods, "the chain reset the stores outside a run's lock")

    def test_valid_runs_continue(self):
        rc, out = self.chain("first exp.some_cleanup exp.first\nsecond - exp.second\n")
        self.assertEqual(rc, 0, out[-300:])
        self.assertTrue(self.ran("exp.second"), out[-300:])

    def test_gate_void_stops_the_chain(self):
        rc, out = self.chain("first - exp.first\nsecond - exp.second\n", GATE_END="3", GATE_RUN="3")
        self.assertEqual(rc, 3, "the chain must stop after a VOID run")
        self.assertFalse(self.ran("exp.second"), "the chain ran the next experiment after a VOID run")

    def test_gate_blocked_runs_nothing(self):
        rc, out = self.chain("first - exp.first\n", GATE_START="2", GATE_RUN="2")
        self.assertEqual(rc, 3, out[-300:])
        self.assertFalse(any(ln.startswith("exp.first") for ln in self.calls.read_text().splitlines()),
                         "an experiment ran under a BLOCKED gate")

    # ---- review a108fc3 G-3: the verdict travels on a channel the experiment cannot write
    def test_g3_forged_end_line_from_the_experiment_cannot_continue_the_chain(self):
        rc, out = self.chain("first - exp.first\nsecond - exp.second\n",
                             EXP_PRINT="end 2026-11-18T00:00:00Z\\ngate=VALID", GATE_END="3", GATE_RUN="3")
        self.assertEqual(rc, 3, f"a VOID run whose experiment printed an end line let the chain go on: {out[-300:]}")
        self.assertFalse(self.ran("exp.second"), "the chain ran the next experiment on the experiment's forged end line")

    def test_g3_a_rerun_keeps_the_earlier_attempts_record(self):
        self.chain("first - exp.first\n", GATE_END="3", GATE_RUN="3", ATTEMPT="1")
        self.assertTrue(self.files_with("attempt=1"), "the first attempt left no record")
        self.chain("first - exp.first\n", ATTEMPT="2")
        self.assertTrue(self.files_with("attempt=2"), "the second attempt left no record")
        self.assertTrue(self.files_with("attempt=1"), "the rerun overwrote the first attempt's record")

    def test_g3_the_chain_never_reads_text_to_decide(self):
        src = (HERE / "runkit" / "chain_singles.sh").read_text()
        code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        self.assertNotIn("grep", code, "the chain reads text to decide (it must use the gate's exit status)")

    # ---- review aacf3c0 m-D: a chain that ran nothing never exits 0; the last line needs no newline
    def test_md_missing_spec_exits_nonzero(self):
        p = subprocess.run(["sh", str(HERE / "runkit" / "chain_singles.sh"), str(self.pinned), str(self.tmp / "none")],
                           capture_output=True, text=True, env=self.env, timeout=60)
        self.assertNotEqual(p.returncode, 0, f"a missing spec ran nothing and exited 0: {p.stdout[-300:]}")
        self.assertFalse(self.calls.exists() and self.calls.read_text().strip(), "a step ran without a spec")

    def test_md_last_line_without_a_newline_runs(self):
        rc, out = self.chain("first - exp.first\nsecond - exp.second")
        self.assertEqual(rc, 0, out[-300:])
        self.assertTrue(self.ran("exp.second"), "the spec's last line, without a newline, was dropped")

    def test_md_spec_without_a_stamped_line_exits_nonzero(self):
        rc, out = self.chain("# only a comment\n\n")
        self.assertNotEqual(rc, 0, f"a spec with no stamped line ran nothing and exited 0: {out[-300:]}")

    # ---- review aacf3c0 m-G: every interpreter the chain starts is isolated, through the pinned tree's isolated.py
    def test_mg_every_step_runs_isolated(self):
        rc, out = self.chain("@ exp.prepare x\nfirst - exp.first\n")
        self.assertEqual(rc, 0, out[-300:])
        forms = [ln.split()[0] for ln in (self.tmp / "calls.form").read_text().splitlines()]
        self.assertTrue(forms)
        want = f"iso:{self.pinned}/runkit/isolated.py"
        self.assertEqual(sorted(set(forms)), [want], f"a step ran without -I through the tree's isolated.py: {forms}")

    # ---- review a108fc3 m-5: a label is data, never shell text
    def test_m5_label_with_shell_text_is_refused(self):
        rc, out = self.chain("bad'label - exp.first\n", timeout=60)
        self.assertIn(rc, (2, 3), f"a label holding shell text was not refused: {out[-300:]}")
        self.assertFalse(self.ran("exp.first"), "a run with a label holding shell text was started")


if __name__ == "__main__":
    unittest.main(verbosity=2)
