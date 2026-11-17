"""Review-3 N2: the sync destination guard of policy/sync.sh must not let a caller-supplied value reach the remote shell.

At 0a20b64 the guard accepted SYNC_DEST=emlab2-16096/policy/runs/x;<command>;#/src and interpolated it into `ssh t2-dev "mkdir -p $DEST"`,
so the remote shell would run <command> outside the allowed tree. The fixed script reads only a run NAME (SYNC_RUN), checks it against an
allow-list, builds the destination from it, refuses symlinked ancestors on the host and creates the run directory atomically.

ssh and rsync are STUBBED here (a script on PATH that records its arguments); nothing reaches a host and no payload is ever executed. Only
for names the guard must accept does the ssh stub run the remote command, inside a temporary sandbox that stands in for the host's home.
Each case sets SYNC_DEST consistently with SYNC_RUN, so the same test runs against the 0a20b64 script (which reads SYNC_DEST) for the
failing-first record and against the fixed script (which reads SYNC_RUN)."""
import os
import shutil
import subprocess
import tempfile
import unittest

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = "INJECTED-N2"
STUB_SSH = """#!/bin/sh
{ echo "CALL ssh"; for a in "$@"; do printf 'ARG[%s]\\n' "$a"; done; } >> "$STUB_LOG"
if [ -n "$STUB_EXEC_ROOT" ]; then for a in "$@"; do last=$a; done; cd "$STUB_EXEC_ROOT" && exec sh -c "$last"; fi
exit 0
"""
STUB_RSYNC = """#!/bin/sh
{ echo "CALL rsync"; for a in "$@"; do printf 'ARG[%s]\\n' "$a"; done; } >> "$STUB_LOG"
exit 0
"""
INJECTIONS = ["x;echo " + MARK + ";#", "x$(echo " + MARK + ")", "x`echo " + MARK + "`", "x\necho " + MARK, "x' ; echo " + MARK + " ; '",
              "x|echo " + MARK, "x&&echo " + MARK, "x/../../" + MARK]
BAD_NAMES = ["", ".hidden", "-rf", "a/b", "a b", "..", "a..b", "x" * 65, "café", "x\t", "*"]


class SyncGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syncguard-")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name, body in (("ssh", STUB_SSH), ("rsync", STUB_RSYNC)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o700)
        self.log = os.path.join(self.tmp, "calls.log")
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, name, exec_remote=False):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), STUB_LOG=self.log, ALLOW_DIRTY="1",
                   SYNC_RUN=name, SYNC_DEST="emlab2-16096/policy/runs/%s/src" % name)
        env.pop("STUB_EXEC_ROOT", None)
        if exec_remote:
            env["STUB_EXEC_ROOT"] = self.home
        self.assertEqual(shutil.which("ssh", path=env["PATH"]), os.path.join(self.bin, "ssh"), "ssh stub not active")
        self.assertEqual(shutil.which("rsync", path=env["PATH"]), os.path.join(self.bin, "rsync"), "rsync stub not active")
        r = subprocess.run(["sh", os.path.join(POLICY, "sync.sh")], env=env, capture_output=True, text=True, timeout=60)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = f.read()
        return r, calls

    def _tree(self, *parts):
        p = os.path.join(self.home, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def test_injected_name_never_reaches_ssh_or_rsync(self):
        for name in INJECTIONS:
            with self.subTest(name=name):
                if os.path.exists(self.log):
                    os.remove(self.log)
                r, calls = self._run(name)
                self.assertNotIn(MARK, calls, "the injected text reached the remote command: %r" % calls)
                self.assertEqual(calls, "", "refused names must not cause any ssh or rsync call")
                self.assertNotEqual(r.returncode, 0)

    def test_bad_names_are_refused_before_any_remote_call(self):
        for name in BAD_NAMES:
            with self.subTest(name=name):
                if os.path.exists(self.log):
                    os.remove(self.log)
                r, calls = self._run(name)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(calls, "")

    def test_symlinked_ancestor_is_refused(self):
        """runs/ is a symlink to a directory outside the tree: nothing is created there and nothing is copied."""
        self._tree("emlab2-16096", "policy")
        outside = self._tree("outside")
        os.symlink(outside, os.path.join(self.home, "emlab2-16096", "policy", "runs"))
        r, calls = self._run("ok-run", exec_remote=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(outside), [], "the run directory was created through a symlinked ancestor")
        self.assertNotIn("CALL rsync", calls)

    def test_existing_run_is_refused(self):
        """One fresh run directory per sync: an existing runs/<name> (even without src) is refused before any copy."""
        self._tree("emlab2-16096", "policy", "runs", "ok-run")
        r, calls = self._run("ok-run", exec_remote=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("CALL rsync", calls)
        self.assertFalse(os.path.exists(os.path.join(self.home, "emlab2-16096", "policy", "runs", "ok-run", "src")))

    def test_valid_name_builds_the_destination_from_the_name(self):
        self._tree("emlab2-16096", "policy", "runs")
        name = "red-0a20b64.n2_1"
        r, calls = self._run(name, exec_remote=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "emlab2-16096", "policy", "runs", name, "src")))
        dest = "t2-dev:emlab2-16096/policy/runs/%s/src/" % name
        self.assertIn("ARG[%s]" % dest, calls)
        self.assertIn("ARG[%sGIT_STAMP.json]" % dest, calls)
        rsync_dests = [ln[4:-1] for ln in calls.splitlines() if ln.startswith("ARG[t2-dev:")]
        self.assertEqual(len(rsync_dests), 2)
        self.assertTrue(all(d.startswith(dest) for d in rsync_dests), rsync_dests)


if __name__ == "__main__":
    unittest.main()
