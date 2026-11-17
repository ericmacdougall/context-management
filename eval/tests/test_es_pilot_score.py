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


WITHHELD = "WITHHELD-INCOMPLETE-EVIDENCE"


def rewrite(tmp, rows_fn=None, ce_fn=None):
    """Edit the fixture's rows and CE records in place (each fn receives and returns the list)."""
    rp, cp = os.path.join(tmp, "rows.jsonl.gz"), os.path.join(tmp, "ce.jsonl")
    rows = [json.loads(l) for l in gzip.open(rp, "rt")]
    ce = [json.loads(l) for l in open(cp)] if os.path.exists(cp) else []
    if rows_fn:
        rows = rows_fn(rows)
    if ce_fn:
        ce = ce_fn(ce)
    with gzip.open(rp, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    if ce:
        with open(cp, "w") as f:
            for c in ce:
                f.write(json.dumps(c) + "\n")
    elif os.path.exists(cp):
        os.remove(cp)


class IncompleteEvidence(unittest.TestCase):
    """The recommendation must never be KEEP (or any positive category) when the evidence is incomplete."""

    def run_case(self, rows_fn=None, ce_fn=None, prior=None):
        with tempfile.TemporaryDirectory() as t:
            make(t, True)
            rewrite(t, rows_fn, ce_fn)
            return sc.score(t, prior)

    def test_complete_fixture_still_keeps(self):
        r = self.run_case()
        self.assertEqual(r["recommendation"]["category"], "KEEP")
        self.assertEqual(r["evidence"]["problems"], [])

    def test_no_cross_encoder_records_withholds(self):
        r = self.run_case(ce_fn=lambda ce: [])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)
        self.assertTrue(r["evidence"]["problems"])

    def test_one_missing_cross_encoder_record_withholds(self):
        r = self.run_case(ce_fn=lambda ce: ce[1:])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_duplicate_cross_encoder_record_withholds(self):
        r = self.run_case(ce_fn=lambda ce: ce + [dict(ce[0], ce_es=list(reversed(ce[0]["ce_es"])))])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_extra_cross_encoder_record_withholds(self):
        r = self.run_case(ce_fn=lambda ce: ce + [dict(ce[0], qid="ghost")])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_duplicate_row_withholds(self):
        r = self.run_case(rows_fn=lambda rows: rows + [rows[0]])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_question_set_differing_across_levels_withholds(self):
        r = self.run_case(rows_fn=lambda rows: [x for x in rows if not (x["qid"] == "q3" and x["level"] == "H1")],
                          ce_fn=lambda ce: [x for x in ce if not (x["qid"] == "q3" and x["level"] == "H1")])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_cross_encoder_head_not_a_permutation_of_its_rrf_head_withholds(self):
        def bad(ce):
            ce = [dict(c) for c in ce]
            ce[0]["ce_exact"] = ce[0]["ce_exact"][:-1] + ["zz:0"]
            return ce
        r = self.run_case(ce_fn=bad)
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_cross_encoder_head_flags_must_match_the_rows(self):
        def bad(ce):
            ce = [dict(c) for c in ce]
            ce[0]["head_same_set"] = False
            return ce
        r = self.run_case(ce_fn=bad)
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_prior_reproduction_path_given_but_absent_is_a_failure_not_a_skip(self):
        r = self.run_case(prior="/nonexistent/es_pilot.json")
        self.assertEqual(r["recommendation"]["category"], WITHHELD)
        self.assertEqual(r["reproduction_prior_es"]["status"], "PATH_ABSENT")

    def test_prior_not_supplied_is_recorded_as_not_checked(self):
        r = self.run_case()
        self.assertEqual(r["reproduction_prior_es"]["status"], "NOT_CHECKED")
        self.assertEqual(r["reproduction_prior_ce"]["status"], "NOT_CHECKED")


if __name__ == "__main__":
    unittest.main()
