"""Offline tests of the run kit's small pieces (review aacf3c0): pin_run.sh (m-J), sync.sh's target home, wp/runlock.py
(F-1: the store tools' side of the run lock) and runkit/isolated.py (m-G). Stubs on PATH stand in for tar and rsync;
nothing reaches a host or a container.
usage (from writepath/): python3 -m unittest tests.test_runkit_scripts -v"""
import fcntl
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
REAL_TAR = shutil.which("tar")

# tar that fails to archive: it writes a valid archive of one file (a partial copy), then exits 2
FAILING_TAR = """#!/bin/sh
m=; out=-; prev=
for a in "$@"; do
  case "$a" in -c) m=c ;; -x) m=x ;; esac
  [ "$prev" = -f ] && out=$a
  prev=$a
done
if [ "$m" = c ]; then
  (cd "$PARTIAL_DIR" && "$REAL_TAR" -c -f "$out" one.txt)
  exit 2
fi
exec "$REAL_TAR" "$@"
"""


class PinRun(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "home" / "src"
        (self.src / "exp").mkdir(parents=True)
        shutil.copyfile(HERE / "pin_run.sh", self.src / "pin_run.sh")
        (self.src / "GIT_HEAD").write_text("abc1234\n")
        (self.src / "exp" / "a.py").write_text("A = 1\n")
        (self.src / "exp" / "b.py").write_text("B = 2\n")
        part = self.tmp / "partial"
        part.mkdir()
        (part / "one.txt").write_text("partial\n")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "tar").write_text(FAILING_TAR)
        (bindir / "tar").chmod(0o755)
        self.bad = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", PARTIAL_DIR=str(part), REAL_TAR=REAL_TAR)

    def pin(self, env=None):
        return subprocess.run(["sh", str(self.src / "pin_run.sh")], capture_output=True, text=True, timeout=60,
                              env=env or dict(os.environ))

    def test_mj_failed_archive_pins_nothing(self):
        p = self.pin(self.bad)
        self.assertNotEqual(p.returncode, 0, f"pin_run exited 0 after its archive failed: {p.stdout} {p.stderr}")
        self.assertEqual(p.stdout.strip(), "", "pin_run printed a pinned directory after its archive failed")
        runs = self.tmp / "home" / "runs"
        left = [x.name for x in runs.iterdir()] if runs.exists() else []
        self.assertEqual(left, [], f"a partial pinned tree was left: {left}")

    def test_pin_copies_the_tree(self):
        p = self.pin()
        self.assertEqual(p.returncode, 0, p.stderr)
        d = (self.src / p.stdout.strip()).resolve()
        self.assertEqual((d / "exp" / "b.py").read_text(), "B = 2\n")
        self.assertEqual(sorted(x.name for x in d.parent.iterdir()), [d.name], "pin_run left its archive behind")


class Sync(unittest.TestCase):
    """sync.sh ships the committed tree to the stream's home by default and to the home WP_REMOTE_HOME names."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        self.log = self.tmp / "rsync.args"
        (bindir / "rsync").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.log}"\n')
        (bindir / "rsync").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")

    def sync(self, **env):
        return subprocess.run(["sh", str(HERE / "sync.sh")], capture_output=True, text=True, timeout=300,
                              env=dict(self.env, **env))

    def target(self):
        return self.log.read_text().splitlines()[-1] if self.log.exists() else None

    def test_sync_ships_to_the_named_home(self):
        p = self.sync(WP_REMOTE_HOME="emlab2-16096/gatefix/wp")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.target(), "t2-dev:emlab2-16096/gatefix/wp/src/")

    def test_sync_default_home_is_the_streams(self):
        p = self.sync()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.target(), "t2-dev:emlab2-16096/wphard/wp/src/")

    def test_sync_refuses_an_absolute_or_escaping_home(self):
        for bad in ("/etc", "emlab2-16096/../x", "a b"):
            p = self.sync(WP_REMOTE_HOME=bad)
            self.assertEqual(p.returncode, 2, f"{bad!r}: {p.stdout} {p.stderr}")
            self.assertIsNone(self.target(), f"sync shipped to {bad!r}")


class RunLock(unittest.TestCase):
    """wp/runlock.py: a store tool runs only with the run lock: its own, or the gate's (WP_RUN_LOCK_FD)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / "conf").mkdir(parents=True)
        (self.home / "state").mkdir()
        for k, v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:1",
                     "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
            (self.home / "conf" / k).write_text(v + "\n")
        self.lockfile = self.home / "state" / "run.lock"

    def hold(self, env=None, pass_fds=()):
        code = "import sys; sys.path.insert(0, %r); from wp import runlock; runlock.hold('t'); print('held')" % str(HERE)
        return subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, WP_HOME=str(self.home), **(env or {})), pass_fds=pass_fds)

    def gate_lock(self):
        f = open(self.lockfile, "a")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)
        return f

    def test_free_lock_is_taken(self):
        p = self.hold()
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "held"), p.stderr)

    def test_held_lock_refuses(self):
        self.gate_lock()
        p = self.hold()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("refused", p.stdout)

    def test_the_gates_own_descriptor_is_accepted(self):
        f = self.gate_lock()
        p = self.hold({"WP_RUN_LOCK_FD": str(f.fileno())}, pass_fds=(f.fileno(),))
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "held"), p.stderr)

    def test_a_fresh_descriptor_of_the_lock_file_is_refused(self):
        self.gate_lock()
        g = open(self.lockfile, "a")                     # another open of the same file: not the gate's lock
        self.addCleanup(g.close)
        p = self.hold({"WP_RUN_LOCK_FD": str(g.fileno())}, pass_fds=(g.fileno(),))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_a_descriptor_of_another_file_is_refused(self):
        self.lockfile.touch()
        other = open(self.tmp / "other", "a")
        self.addCleanup(other.close)
        p = self.hold({"WP_RUN_LOCK_FD": str(other.fileno())}, pass_fds=(other.fileno(),))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("does not refer to the run lock", p.stdout)


class Isolated(unittest.TestCase):
    """runkit/isolated.py runs a module of its own tree only, and only under -I -B."""

    def run_iso(self, *args, flags=("-I", "-B")):
        return subprocess.run([sys.executable, *flags, str(HERE / "runkit" / "isolated.py"), *args],
                              capture_output=True, text=True, timeout=60, cwd=str(HERE.parent),
                              env=dict(os.environ, PYTHONPATH="/nonexistent"))

    def test_runs_a_module_of_its_tree_isolated(self):
        p = self.run_iso("exp.guard_policy")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_refuses_without_isolation(self):
        p = self.run_iso("exp.guard_policy", flags=("-B",))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("python -I -B", p.stdout)

    def test_refuses_a_module_outside_its_tree(self):
        for mod in ("json", "exp.no_such_module", "../exp", "exp/guard_policy"):
            p = self.run_iso(mod)
            self.assertEqual(p.returncode, 2, f"{mod}: {p.stdout} {p.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
