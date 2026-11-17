"""Review-3 N6: `python -m verify` itself enforces the provenance of a STAMPED set (one result per experiment, clean stamps made at the
thresholds commit, one commit, one thresholds digest equal to the THRESHOLDS file, the STAMPED seeds). At 0a20b64 it checked only item
hashes and recomputed verdicts, so a dirty, mixed or wrongly seeded set verified PASS (the check lived in the owner's work dir).

The entry point is loaded by path with its experiment table replaced by ONE synthetic experiment whose verifier passes any non-empty
artifact, so only provenance decides. Stdlib only; no database."""
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THR = b"# thresholds\namendment N\n"


def _main_module():
    spec = importlib.util.spec_from_file_location("verify_main_under_test", os.path.join(POLICY, "verify", "__main__.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prov-")
        self.vdir = os.path.join(self.tmp, "verify")
        os.mkdir(self.vdir)
        with open(os.path.join(self.vdir, "x.py"), "w") as f:
            f.write("def verify(doc, rows):\n    return {'verdict': 'PASS' if rows else 'FAIL', 'checks': {}}\n")
        real = os.path.join(POLICY, "verify", "provenance.py")
        if os.path.exists(real):
            shutil.copy(real, self.vdir)
        self.seeds = os.path.join(self.tmp, "seeds.py")
        with open(self.seeds, "w") as f:
            f.write("STAMPED = {'x': [1, 2]}\nDEV = {'x': [7]}\n")
        self.thr = os.path.join(self.tmp, "THRESHOLDS.md")
        with open(self.thr, "wb") as f:
            f.write(THR)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set(self, n=1, dev=False, **change):
        res = tempfile.mkdtemp(dir=self.tmp)
        raw = gzip.compress(b'{"row": 1}\n', mtime=0)
        h = hashlib.sha256(raw).hexdigest()
        tag = "DEV." if dev else ""
        items = "%sx_exp.items.%s.jsonl.gz" % (tag, h[:16])
        with open(os.path.join(res, items), "wb") as f:
            f.write(raw)
        for i in range(n):
            stamp = {"commit": "c1", "thresholds_commit": "c1", "dirty": False, "thresholds_sha256": hashlib.sha256(THR).hexdigest()}
            doc = {"name": "x_exp", "stamp": dict(stamp, **change.get("stamp", {})), "params": {"seeds": change.get("seeds", [1, 2])},
                   "result": {"verdict": "PASS", "items": {"file": items, "sha256": h}}}
            with open(os.path.join(res, "%sx_exp.%016d.json" % (tag, i)), "w") as f:
                json.dump(doc, f)
        return res

    def _verify(self, res, dev=False):
        m = _main_module()
        m.EXPS, m.HERE = {"x": "x_exp"}, self.vdir
        argv = [res] + (["--dev"] if dev else []) + ["--seeds-file", self.seeds, "--thresholds-file", self.thr]
        return m.main(argv)

    def test_good_stamped_set_passes(self):
        self.assertEqual(self._verify(self._set()), 0)

    def test_dirty_stamp_fails(self):
        self.assertEqual(self._verify(self._set(stamp={"dirty": True})), 1)

    def test_run_not_at_the_thresholds_commit_fails(self):
        self.assertEqual(self._verify(self._set(stamp={"thresholds_commit": "c0"})), 1)

    def test_wrong_seeds_fail(self):
        self.assertEqual(self._verify(self._set(seeds=[1, 3])), 1)
        self.assertEqual(self._verify(self._set(seeds=[7])), 1)                    # DEV seeds in a stamped set

    def test_two_result_files_fail(self):
        self.assertEqual(self._verify(self._set(n=2)), 1)

    def test_thresholds_digest_must_match_the_file(self):
        self.assertEqual(self._verify(self._set(stamp={"thresholds_sha256": "0" * 64})), 1)

    def test_seedless_experiment_must_record_no_seeds(self):
        """b1 never draws seeds: its stamped result records none (the reserved STAMPED entry is unused); recording some is a mismatch."""
        shutil.copy(os.path.join(self.vdir, "x.py"), os.path.join(self.vdir, "b1.py"))
        with open(self.seeds, "w") as f:
            f.write("STAMPED = {'b1': [41]}\n")

        def run(res):
            m = _main_module()
            m.EXPS, m.HERE = {"b1": "x_exp"}, self.vdir
            return m.main([res, "--seeds-file", self.seeds, "--thresholds-file", self.thr])
        res = self._set()
        for f in os.listdir(res):
            if f.startswith("x_exp.") and ".items." not in f:
                p = os.path.join(res, f)
                with open(p) as fh:
                    d = json.load(fh)
                del d["params"]["seeds"]
                with open(p, "w") as fh:
                    json.dump(d, fh)
        self.assertEqual(run(res), 0)
        self.assertEqual(run(self._set(seeds=[41])), 1)

    def test_dev_set_is_not_held_to_provenance(self):
        self.assertEqual(self._verify(self._set(dev=True, stamp={"dirty": True}, seeds=[7]), dev=True), 0)

    def test_verify_record_keeps_rows_and_provenance_apart(self):
        res = self._set(stamp={"dirty": True})
        self._verify(res)
        rec = [f for f in os.listdir(res) if f.startswith("VERIFY.")]
        self.assertEqual(len(rec), 1)
        with open(os.path.join(res, rec[0])) as f:
            doc = json.load(f)
        self.assertEqual((doc.get("overall"), doc.get("rows_pass"), (doc.get("provenance") or {}).get("ok")), ("FAIL", True, False))


if __name__ == "__main__":
    unittest.main()
