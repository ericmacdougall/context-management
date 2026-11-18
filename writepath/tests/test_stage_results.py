"""Offline test of runkit/stage_results.py (review a108fc3 G-2, GATE-DESIGN 8.2 P8): a result file is staged only when
a VALID end record of the gate's ledger (a copy, verified by its hash chain) lists that file of that run with that
SHA-256. A file the gate did not observe in the run's directory, a VOID run, a file changed after the run and an edited
ledger are each refused for that reason, and nothing is written. All values are generated here.
usage (from writepath/): python3 -m unittest tests.test_stage_results -v"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
TOOL = os.environ.get("STAGE_RESULTS_UNDER_TEST") or str(HERE / "runkit" / "stage_results.py")
RUN = "r-" + "a" * 32
COMMIT = "c0ffee1" + "0" * 33


def chain(recs):
    """Ledger lines as the gate's Ledger writes them: seq, prev (SHA-256 of the previous line), sorted keys."""
    out, prev = [], "0" * 64
    for i, r in enumerate(recs):
        ln = json.dumps(dict(r, seq=i + 1, prev=prev, at="2026-11-18T00:00:00Z"), sort_keys=True).encode()
        out.append(ln)
        prev = hashlib.sha256(ln).hexdigest()
    return b"\n".join(out) + b"\n"


class Stage(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = "/home/someone/emlab2-16096/wphard/wp/"
        self.runs = self.tmp / "runs"
        d = self.runs / RUN
        d.mkdir(parents=True)
        self.row = json.dumps({"name": "e_verify", "gate_run": RUN, "env": {"code_commit": COMMIT[:7]},
                               "result": {"home": self.home + "results"}}).encode()
        (d / "e_verify.0123456789abcdef.json").write_bytes(self.row)
        (d / "experiment.out").write_bytes(b"RESULT e_verify\n")
        self.files = [{"name": n, "sha256": hashlib.sha256((d / n).read_bytes()).hexdigest()}
                      for n in ("e_verify.0123456789abcdef.json", "experiment.out")]
        self.dst = self.tmp / "dst"
        self.dst.mkdir()
        self.groups = self.tmp / "groups.tsv"
        self.groups.write_text("group\trun\tfile\tlabel\ng1\t%s\te_verify.0123456789abcdef.json\te_verify\n" % RUN)

    def ledger(self, verdict="VALID", files=None, edit=False):
        recs = [{"kind": "init", "v": 5, "source": "init", "reason": "test"},
                {"kind": "admit", "v": 5, "run": RUN, "es": "e" * 64, "pg": "f" * 64, "commit": COMMIT},
                {"kind": "end", "run": RUN, "verdict": verdict, "files": self.files if files is None else files,
                 "commit": COMMIT, "gate": "disk_gate v5"}]
        raw = chain(recs)
        lines = raw.split(b"\n")[:-1]
        (self.tmp / "stop_ledger.head").write_text(json.dumps(        # the gate's head anchor, copied with the ledger
            {"count": len(lines), "tip": hashlib.sha256(lines[-1]).hexdigest(), "at": "2026-11-18T00:00:00Z"}))
        if edit:
            raw = raw.replace(b'"verdict": "VOID"', b'"verdict": "VALID"')
        p = self.tmp / "stop_ledger.jsonl"
        p.write_bytes(raw)
        return p

    def stage(self, ledger):
        r = subprocess.run([sys.executable, TOOL, str(self.groups), str(self.runs), str(ledger), str(self.dst)],
                           capture_output=True, text=True, env=dict(os.environ, HOST_HOME=self.home), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def staged(self):
        return [p for p in self.dst.rglob("*") if p.is_file()]

    def test_file_listed_by_a_valid_end_record_is_staged(self):
        rc, out = self.stage(self.ledger())
        self.assertEqual(rc, 0, out[-400:])
        got = [p for p in self.staged() if p.name == "e_verify.0123456789abcdef.json"]
        self.assertEqual(len(got), 1, out[-300:])
        self.assertNotIn(self.home.encode(), got[0].read_bytes(), "the host home path was committed")
        man = (self.dst / "MANIFEST.tsv").read_text()
        self.assertIn(RUN, man, "the manifest does not name the run that produced the file")

    def assertRefused(self, rc, out, why):
        self.assertNotEqual(rc, 0, f"staged although {why}: {out[-300:]}")
        self.assertIn("refused", out, f"not refused for the reason under test ({why}): {out[-300:]}")
        self.assertEqual([p for p in self.staged() if p.suffix == ".json"], [], "a refused set wrote a result file")

    def test_file_the_gate_did_not_list_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(files=[f for f in self.files if f["name"] == "experiment.out"])),
                           "the end record does not list the file")

    def test_file_of_a_void_run_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(verdict="VOID")), "the run is VOID")

    def test_file_changed_after_the_run_is_refused(self):
        (self.runs / RUN / "e_verify.0123456789abcdef.json").write_bytes(self.row.replace(b"e_verify", b"e_verifz"))
        self.assertRefused(*self.stage(self.ledger()), "the file changed after the gate hashed it")

    def test_edited_ledger_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(verdict="VOID", edit=True)), "the ledger copy was edited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
