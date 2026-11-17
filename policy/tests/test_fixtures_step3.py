"""The step-3 fixture exporter and checker (exp/fixtures_step3.py): a small set exported here must replay clean through the oracle, the
export must be byte-deterministic, and the checker must catch an edited expectation, an edited file and a mutated oracle (so a clean
check cannot be vacuous). The committed set is checked in tests/test_fixtures_step3_set.py."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

from exp import fixtures_step3 as F

SMALL = {"b2_seeds": ["fx-test"], "per_seed": 3, "b1_depths": {"r1": 2, "r3": 3, "caps": 3}}


class FixturesStep3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.setrecursionlimit(10000)
        cls.dir = tempfile.mkdtemp(prefix="fx3-")
        cls.man = F.export(cls.dir, **SMALL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _copy(self):
        d = tempfile.mkdtemp(prefix="fx3c-")
        self.addCleanup(shutil.rmtree, d, True)
        for n in ("b2.jsonl.gz", "b1.jsonl.gz", "MANIFEST.json"):
            shutil.copy(os.path.join(self.dir, n), d)
        return d

    def _rewrite(self, d, name, edit):
        p = os.path.join(d, name)
        recs = list(F._read(p))
        edit(recs)
        F._write(p, recs)
        with open(os.path.join(d, "MANIFEST.json"), encoding="utf-8") as f:     # re-seal, so only the replay can catch the edit
            man = json.load(f)
        man["b2" if name.startswith("b2") else "b1"]["sha256"] = F._sha_file(p)
        with open(os.path.join(d, "MANIFEST.json"), "w", encoding="utf-8") as f:
            json.dump(man, f)

    def test_small_set_replays_clean(self):
        rep = F.Report()
        F.check_manifest(self.dir, rep)
        F.check_oracle(self.dir, None, rep)
        self.assertEqual((rep.mismatches, rep.first), (0, []))
        self.assertEqual(rep.counts["b2_logs"], 3)
        self.assertEqual(rep.counts["b2_events"], self.man["b2"]["events"])
        self.assertEqual(rep.counts["b1_edges"], sum(s["edges"] for s in self.man["b1"]["scenarios"].values()))
        self.assertEqual(rep.counts["b1_nodes"], sum(s["nodes"] for s in self.man["b1"]["scenarios"].values()))

    def test_export_is_byte_deterministic(self):
        d = tempfile.mkdtemp(prefix="fx3d-")
        self.addCleanup(shutil.rmtree, d, True)
        man = F.export(d, **SMALL)
        self.assertEqual((man["b2"]["sha256"], man["b1"]["sha256"]), (self.man["b2"]["sha256"], self.man["b1"]["sha256"]))

    def test_edited_expected_code_is_caught(self):
        d = self._copy()

        def edit(recs):
            ev, code, orig = recs[0]["events"][0]
            recs[0]["events"][0] = [ev, "proposed" if code != "proposed" else "stale", orig]
        self._rewrite(d, "b2.jsonl.gz", edit)
        rep = F.Report()
        F.check_manifest(d, rep)
        self.assertEqual(rep.mismatches, 0)
        F.check_oracle(d, None, rep)
        self.assertGreaterEqual(rep.mismatches, 1)

    def test_edited_b1_edge_is_caught(self):
        d = self._copy()

        def edit(recs):
            node = recs[0]["nodes"][recs[0]["root"]]
            ev, code, orig, child = node["edges"][0]
            node["edges"][0] = [ev, "unauthorized" if code != "unauthorized" else "stale", orig, child]
        self._rewrite(d, "b1.jsonl.gz", edit)
        rep = F.check_oracle(d)
        self.assertGreaterEqual(rep.mismatches, 1)

    def test_edited_view_is_caught(self):
        d = self._copy()

        def edit(recs):
            recs[0]["view"] = dict(recs[0]["view"], revoked=["someone"])
        self._rewrite(d, "b2.jsonl.gz", edit)
        self.assertGreaterEqual(F.check_oracle(d).mismatches, 1)

    def test_edited_file_fails_the_manifest(self):
        d = self._copy()
        p = os.path.join(d, "b2.jsonl.gz")
        with gzip.open(p, "rt", encoding="utf-8") as f:
            lines = f.readlines()
        F._write(p, [json.loads(x) for x in lines[1:]])                         # a log dropped, manifest NOT re-sealed
        rep = F.Report()
        F.check_manifest(d, rep)
        self.assertEqual(rep.mismatches, 1)

    def test_mutated_oracle_is_caught(self):
        for control in ("agent_may_decide", "stale_unchecked", "idempotency_removed"):
            with self.subTest(control=control):
                self.assertGreater(F.check_oracle(self.dir, control).mismatches, 0)


if __name__ == "__main__":
    unittest.main()
