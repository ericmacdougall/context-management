import gzip
import json
import os
import tempfile
import unittest

from emlab import es_pilot_score as sc
from emlab.es_pilot2 import derange_across_clusters, level_filter, rollup
from emlab.es_pilot_score import classify, project

LV = ("H0", "H1", "H2", "H3")


def lists(good_first):
    # rounds "s<i>:0"; session s0 is gold; good_first puts it at rank 1, otherwise at rank 30 (outside the top 10 sessions)
    ids = ["s%d:0" % i for i in range(1, 40)]
    return (["s0:0"] + ids) if good_first else (ids[:29] + ["s0:0"] + ids[29:])


def make(tmp, es_good):
    rows, ce = [], []
    for i in range(8):
        for lv in LV:
            ex = {a: lists(True) for a in ("bm25", "dense", "rrf")}
            es = {a: lists(es_good) for a in ("bm25", "dense", "rrf")}
            rows.append({"qid": "q%d" % i, "cluster": "c%d" % i, "level": lv, "gold": ["s0"], "exact": ex, "es": es,
                         "ctl_whitespace": lists(False), "ctl_keyword": [], "wrong_qid": "q%d" % ((i + 1) % 8), "wrong_exact": {a: lists(False) for a in ex},
                         "wrong_es": {a: lists(False) for a in ex}})
            ce.append({"qid": "q%d" % i, "level": lv, "ce_exact": ex["rrf"][:20], "ce_es": es["rrf"][:20], "head_same_set": es_good, "head_same_order": es_good})
    with gzip.open(os.path.join(tmp, "rows.jsonl.gz"), "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(tmp, "ce.jsonl"), "w") as f:
        for c in ce:
            f.write(json.dumps(c) + "\n")


class Score(unittest.TestCase):
    def test_identical_engines_hold_and_recommend_keep(self):
        with tempfile.TemporaryDirectory() as t:
            make(t, True)
            r = sc.score(t)
            self.assertEqual(r["recommendation"]["category"], "KEEP")
            self.assertTrue(all(v["mean_diff"] == 0 for lv in LV for v in r["levels"][lv]["arms"].values()))
            self.assertTrue(r["control_checks"]["whitespace_H0_le_-0.10"])
            self.assertTrue(r["control_checks"]["keyword_le_0.05_all_levels"])

    def test_broken_es_is_a_mean_fail_and_wrong_query_control_detects_nothing_when_real_is_equally_bad(self):
        with tempfile.TemporaryDirectory() as t:
            make(t, False)  # ES puts the gold session outside the top 10: a defect of -1.0
            r = sc.score(t)
            self.assertEqual(r["recommendation"]["category"], "MEAN-FAIL")
            self.assertEqual(r["levels"]["H0"]["arms"]["bm25"]["class"], "MEAN-FAIL")
            self.assertFalse(r["control_checks"]["wrong_query_half_all"])  # real ES recall 0 -> the wrong-query comparison cannot pass

    def test_classes_and_projection(self):
        self.assertEqual(classify(0.0, -0.02, 0.02), "HOLDS")
        self.assertEqual(classify(0.031, 0.0, 0.05), "MEAN-FAIL")
        self.assertEqual(classify(0.016, -0.022, 0.054), "WIDTH-FAIL")  # the prior pilot's BM25 H3 cell
        p = project(0.016, -0.022, 0.054, 299)
        self.assertAlmostEqual(p["half_width"], 0.038 * (94 / 299) ** 0.5, places=6)
        self.assertTrue(p["inside"])

    def test_derangement_crosses_clusters_and_filter_shapes(self):
        qids = ["a", "b", "c", "d", "e", "f"]
        cl = {"a": "1", "b": "1", "c": "2", "d": "2", "e": "3", "f": "3"}
        m = derange_across_clusters(qids, cl, 16096)
        self.assertTrue(all(cl[k] != cl[v] for k, v in m.items()))
        self.assertEqual(sorted(m.values()), sorted(qids))
        self.assertEqual(level_filter("H1", ["x", "y"], {"x", "y", "z"}), {"terms": {"session_id": ["x", "y"]}})
        self.assertEqual(level_filter("H3", ["x"], {"x", "y", "z"}), {"bool": {"must_not": [{"terms": {"session_id": ["y", "z"]}}]}})
        self.assertEqual(level_filter("H3", ["x"], {"x"}), {"match_all": {}})
        self.assertEqual(rollup([{"_source": {"round_id": r}} for r in ("a", "a", "b", "c")], 2), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
