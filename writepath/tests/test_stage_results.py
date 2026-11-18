"""Offline test of runkit/stage_results.py (review a108fc3 G-2, GATE-DESIGN 8.2 P8; review aacf3c0 m-E, H-9, F-2): a
result file is staged only from a SET that passes clearance verify-copy, and only when the one VALID end record of a
gate-v6 run in the set's ledger copy (verified by its hash chain) lists that file with that SHA-256 and an experiment
exit status >= 0. A file the gate did not list, a VOID run, a file changed after the run, an edited ledger, an
uncleared set, a pre-v6 run, a signal-killed experiment, a header-only groups file, a group that is a path and a file
staged before are each refused for that reason, and nothing is written. code_commit comes from the gate's records.
Every test reseals the set (a fresh receipt over its files) before staging, so a refusal comes from the condition
under test, not from clearance. All values are generated here.
usage (from writepath/): python3 -m unittest tests.test_stage_results -v"""
import hashlib
import importlib.util
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


def _clearance():
    spec = importlib.util.spec_from_file_location("clearance_for_tests", HERE / "runkit" / "clearance.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Stage(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = "/home/someone/emlab2-16096/wphard/wp/"
        self.set = self.tmp / "set-20261118"
        self.runs = self.set / "runs"
        d = self.runs / RUN
        d.mkdir(parents=True)
        (self.set / "ledger").mkdir()
        self.row = json.dumps({"name": "e_verify", "gate_run": RUN, "env": {"code_commit": "deadbee"},
                               "result": {"home": self.home + "results"}}).encode()
        (d / "e_verify.0123456789abcdef.json").write_bytes(self.row)
        (d / "experiment.out").write_bytes(b"RESULT e_verify\n")
        self.files = [{"name": n, "sha256": hashlib.sha256((d / n).read_bytes()).hexdigest()}
                      for n in ("e_verify.0123456789abcdef.json", "experiment.out")]
        self.dst = self.tmp / "dst"
        self.dst.mkdir()
        (self.dst / "MANIFEST.tsv").write_text("group\tfile\tcode_commit\n(pre-v5 rows)\n")
        self.groups = self.tmp / "groups.tsv"
        self.groups.write_text("group\trun\tfile\tlabel\ng1\t%s\te_verify.0123456789abcdef.json\te_verify\n" % RUN)

    def ledger(self, verdict="VALID", files=None, edit=False, v=6, gate="disk_gate v6", exp_rc=0):
        recs = [{"kind": "init", "v": v, "source": "init", "reason": "test"},
                {"kind": "admit", "v": v, "run": RUN, "es": "e" * 64, "pg": "f" * 64, "commit": COMMIT},
                {"kind": "end", "run": RUN, "verdict": verdict, "files": self.files if files is None else files,
                 "commit": COMMIT, "gate": gate, "exp_rc": exp_rc}]
        raw = chain(recs)
        lines = raw.split(b"\n")[:-1]
        (self.set / "ledger" / "stop_ledger.head").write_text(json.dumps(   # the head anchor, copied with the ledger
            {"count": len(lines), "tip": hashlib.sha256(lines[-1]).hexdigest(), "at": "2026-11-18T00:00:00Z"}))
        if edit:
            raw = raw.replace(b'"verdict": "VOID"', b'"verdict": "VALID"')
        p = self.set / "ledger" / "stop_ledger.jsonl"
        p.write_bytes(raw)
        return p

    def clear(self):
        """A teardown receipt, its seal and a PASS stage-check over the set's files as they are now (what clearance
        writes on the host for a cleared set)."""
        cl = _clearance()
        m, bad = cl.manifest(self.set)
        self.assertEqual(bad, [])
        seal = json.dumps({"kind": "seal", "manifest": m}).encode()
        (self.set / "CLEARANCE.seal.json").write_bytes(seal)
        rec = {"kind": "teardown", "torn_down_at": "2026-11-18T01:00:00Z", "manifest": m, "scan": {"exit": 0},
               "tool": {"clearance": "4"}, "keystore": {"covered": True}, "daemon": {"id": "TESTDAEMON"},
               "container_sources": {"e" * 64: {"store": True, "proven": True}},
               "seal_sha256": hashlib.sha256(seal).hexdigest()}
        raw = json.dumps(rec, sort_keys=True).encode()
        (self.set / "CLEARANCE.json").write_bytes(raw)
        (self.set / "CLEARANCE.check.json").write_text(json.dumps(
            {"kind": "stage-check", "result": "PASS", "receipt_sha256": hashlib.sha256(raw).hexdigest()}))

    def stage(self, ledger, clear=True):
        if clear:
            self.clear()
        r = subprocess.run([sys.executable, TOOL, str(self.groups), str(self.runs), str(ledger), str(self.dst)],
                           capture_output=True, text=True, env=dict(os.environ, HOST_HOME=self.home), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def staged(self):
        return [p for p in self.dst.rglob("*") if p.is_file() and p.parent != self.dst]

    def test_file_listed_by_a_valid_end_record_is_staged(self):
        rc, out = self.stage(self.ledger())
        self.assertEqual(rc, 0, out[-400:])
        got = [p for p in self.staged() if p.name == "e_verify.0123456789abcdef.json"]
        self.assertEqual(len(got), 1, out[-300:])
        self.assertNotIn(self.home.encode(), got[0].read_bytes(), "the host home path was committed")
        man = (self.dst / "MANIFEST.v6.tsv").read_text()
        self.assertIn(RUN, man, "the manifest does not name the run that produced the file")
        self.assertEqual(sorted(p.name for p in (self.dst / "clearance" / self.set.name).iterdir()),
                         ["CLEARANCE.check.json", "CLEARANCE.json", "CLEARANCE.seal.json"],
                         "the staged set does not carry its clearance files (H-9)")

    def assertRefused(self, rc, out, why, reason):
        self.assertNotEqual(rc, 0, f"staged although {why}: {out[-300:]}")
        self.assertIn("refused", out, f"not refused for the reason under test ({why}): {out[-300:]}")
        self.assertIn(reason, out, f"refused, but not for the reason under test ({why}): {out[-300:]}")
        self.assertEqual([p for p in self.staged() if p.suffix == ".json"], [], "a refused set wrote a result file")
        self.assertEqual((self.dst / "MANIFEST.tsv").read_text(), "group\tfile\tcode_commit\n(pre-v5 rows)\n",
                         "the pre-v5 MANIFEST.tsv was changed")

    def test_file_the_gate_did_not_list_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(files=[f for f in self.files if f["name"] == "experiment.out"])),
                           "the end record does not list the file", "is not listed by run")

    def test_file_of_a_void_run_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(verdict="VOID")), "the run is VOID", "not VALID")

    def test_file_changed_after_the_run_is_refused(self):
        (self.runs / RUN / "e_verify.0123456789abcdef.json").write_bytes(self.row.replace(b"e_verify", b"e_verifz"))
        self.assertRefused(*self.stage(self.ledger()), "the file changed after the gate hashed it", "differs from")

    def test_edited_ledger_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(verdict="VOID", edit=True)), "the ledger copy was edited",
                           "head anchor")

    # ---- review aacf3c0 m-E: header-only input, code_commit, group paths, overwrites, MANIFEST.tsv
    def test_me_header_only_groups_file_stages_nothing_and_writes_no_manifest(self):
        self.groups.write_text("group\trun\tfile\tlabel\n")
        self.assertRefused(*self.stage(self.ledger()), "the groups file lists no file", "lists no file")
        self.assertFalse((self.dst / "MANIFEST.v6.tsv").exists())

    def test_me_code_commit_is_the_gates_not_the_rows(self):
        rc, out = self.stage(self.ledger())
        self.assertEqual(rc, 0, out[-400:])
        p = self.dst / "MANIFEST.v6.tsv"
        rows = [ln.split("\t") for ln in p.read_text().splitlines()[1:]] if p.exists() else []
        self.assertEqual([r[3] for r in rows], [COMMIT], "code_commit was not taken from the gate's records")

    def test_me_group_that_is_a_path_is_refused(self):
        for g in ("../outside", "/abs", "a/b", ".."):
            self.groups.write_text("group\trun\tfile\tlabel\n%s\t%s\te_verify.0123456789abcdef.json\tx\n" % (g, RUN))
            self.assertRefused(*self.stage(self.ledger()), f"group {g!r}", "is not a plain name")
        self.assertFalse((self.tmp / "outside").exists(), "a file was written outside the destination")

    def test_me_staged_file_is_never_overwritten(self):
        rc, out = self.stage(self.ledger())
        self.assertEqual(rc, 0, out[-400:])
        p = self.dst / "g1" / "e_verify.0123456789abcdef.json"
        before = p.read_bytes()
        rc, out = self.stage(self.ledger())
        self.assertNotEqual(rc, 0, f"a second staging of the same file succeeded: {out[-300:]}")
        self.assertIn("already staged", out)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(len((self.dst / "MANIFEST.v6.tsv").read_text().splitlines()), 2)

    # ---- H-9: only a cleared set is staged
    def test_h9_set_without_a_passing_check_is_refused(self):
        led = self.ledger()
        self.clear()
        (self.set / "CLEARANCE.check.json").unlink()
        self.assertRefused(*self.stage(led, clear=False), "the set has no PASS stage-check", "verify-copy")

    def test_h9_set_changed_after_clearance_is_refused(self):
        led = self.ledger()
        self.clear()
        (self.runs / RUN / "experiment.out").write_bytes(b"edited after clearance\n")
        self.assertRefused(*self.stage(led, clear=False), "a file changed after clearance", "verify-copy")

    # ---- F-2 and gate v6: a signal-killed experiment and a run of an older gate are refused
    def test_f2_experiment_killed_by_a_signal_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(exp_rc=-9)), "the experiment died by signal 9", "did not exit by")

    def test_f2_missing_exit_status_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(exp_rc=None)), "no exit status", "did not exit by")

    def test_v5_run_is_refused(self):
        self.assertRefused(*self.stage(self.ledger(v=5, gate="disk_gate v5")), "a v5 run", "older than v6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
