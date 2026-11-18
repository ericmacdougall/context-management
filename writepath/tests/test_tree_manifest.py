"""Offline test of runkit/tree_manifest.py (review a108fc3 G-2, GATE-DESIGN 8.2 P4) against a real temporary git
repository: the manifest comes from the commit's objects, never from the working tree (a dirty or untracked file
changes nothing in it); it leaves out the top-level results/ directory, as sync.sh ships it; it is the same text the
pinned tree's own files give (the format the gate and tests/test_disk_gate.py read); and `check` recomputes it.
usage (from writepath/): python3 -m unittest tests.test_tree_manifest -v"""
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
TOOL = HERE / "runkit" / "tree_manifest.py"
sys.path.insert(0, str(HERE / "tests"))
from test_disk_gate import _sha256, write_manifest  # noqa: E402


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1", HOME=str(repo))
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, env=env).stdout


class Manifest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        w = self.repo / "writepath"
        for rel, text in (("exp/a.py", "A = 1\n"), ("wp/b.py", "B = 2\n"), ("results/r.json", "{}\n"),
                          ("run.sh", "#!/bin/sh\necho hi\n"), ("other/results/kept.txt", "kept\n")):
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_text(text)
        (w / "run.sh").chmod(0o755)
        os.symlink("exp/a.py", w / "link.py")
        (self.repo / "outside.txt").write_text("not writepath\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "t")
        self.commit = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.w = w

    def tool(self, *args):
        r = subprocess.run([sys.executable, str(TOOL), *args], cwd=self.w, capture_output=True, timeout=60)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def test_manifest_comes_from_the_commit_not_the_working_tree(self):
        rc, clean, err = self.tool("build", self.commit)
        self.assertEqual(rc, 0, err)
        (self.w / "exp" / "a.py").write_text("A = 'dirty'\n")
        (self.w / "exp" / "untracked.py").write_text("U = 1\n")
        rc, dirty, err = self.tool("build", self.commit)
        self.assertEqual(rc, 0, err)
        self.assertEqual(dirty, clean, "a dirty or untracked working-tree file changed the manifest")
        self.assertIn(f"commit {self.commit}\n", clean)
        self.assertIn(f"{_sha256(b'A = 1' + bytes([10]))} 100644 exp/a.py\n", clean)
        self.assertIn(" 100755 run.sh\n", clean)
        self.assertIn(f"{_sha256(b'exp/a.py')} 120000 link.py\n", clean)
        self.assertNotIn("results/r.json", clean, "the top-level results directory is not shipped")
        self.assertIn("other/results/kept.txt", clean, "only the top-level results directory is left out")
        self.assertNotIn("outside", clean)

    def test_manifest_equals_the_shipped_trees_own_manifest(self):
        rc, built, err = self.tool("build", self.commit)
        self.assertEqual(rc, 0, err)
        rc, _, err = self.tool("ship", self.commit, str(self.tmp / "ship.tar"))
        self.assertEqual(rc, 0, err)
        d = self.tmp / "shipped"
        with tarfile.open(self.tmp / "ship.tar") as t:
            t.extractall(d)
        self.assertEqual((d / "TREE.manifest").read_text(), built, "the shipped tree carries another manifest")
        self.assertEqual((d / "GIT_HEAD").read_text().strip(), self.commit[:12])
        ref = self.tmp / "ref"
        shutil.copytree(d, ref, symlinks=True)
        write_manifest(ref, self.commit)                   # the reader's view of the shipped files
        self.assertEqual((ref / "TREE.manifest").read_text(), built)
        self.assertFalse((d / "results").exists(), "the results directory was shipped")

    def test_check_recomputes_a_recorded_manifest_hash(self):
        rc, built, err = self.tool("build", self.commit)
        self.assertEqual(rc, 0, err)
        rc, _, _ = self.tool("check", self.commit, _sha256(built.encode()))
        self.assertEqual(rc, 0)
        rc, _, _ = self.tool("check", self.commit, "0" * 64)
        self.assertEqual(rc, 1, "check accepted a manifest hash that is not the commit's")


if __name__ == "__main__":
    unittest.main(verbosity=2)
