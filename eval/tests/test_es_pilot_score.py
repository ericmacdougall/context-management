import gzip
import hashlib
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
            rows.append({"qid": "q%d" % i, "cluster": "c%d" % i, "level": lv, "n_level_sessions": 50 * (1 + 10 * LV.index(lv)) + LV.index(lv), "gold": ["s0"], "exact": ex, "es": es,
                         "ctl_whitespace": lists(False), "ctl_keyword": [], "wrong_qid": "q%d" % ((i + 1) % 8), "wrong_exact": {a: lists(False) for a in ex},
                         "wrong_es": {a: lists(False) for a in ex}})
            hx, he = ex["rrf"][:20], es["rrf"][:20]
            union = hx + [d for d in he if d not in hx]
            sc_ = {d: -float(j) for j, d in enumerate(union)}  # one score per round, as the real run records it
            ce.append({"qid": "q%d" % i, "level": lv, "ce_exact": sorted(hx, key=lambda d: (-sc_[d], hx.index(d))), "ce_es": sorted(he, key=lambda d: (-sc_[d], he.index(d))),
                       "head_same_set": es_good, "head_same_order": es_good, "scores": sc_})
    with gzip.open(os.path.join(tmp, "rows.jsonl.gz"), "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(tmp, "ce.jsonl"), "w") as f:
        for c in ce:
            f.write(json.dumps(c) + "\n")
    write_manifest(tmp, fresh=True)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def write_manifest(tmp, fresh=False):
    """The expected pilot (q0..q7 in clusters c0..c7) and the sha256 of the artifacts; `fresh` False keeps the expected sets and only re-hashes."""
    mp = os.path.join(tmp, "pilot_manifest.json")
    if fresh or not os.path.exists(mp):
        m = {"version": 1, "source": "test fixture", "qids": ["q%d" % i for i in range(8)], "clusters": {"q%d" % i: "c%d" % i for i in range(8)},
             "levels": list(LV), "arms": ["bm25", "dense", "rrf", "ce"]}
    else:
        m = json.load(open(mp))
    m["artifacts"] = {n: sha(os.path.join(tmp, n)) for n in ("rows.jsonl.gz", "ce.jsonl") if os.path.exists(os.path.join(tmp, n))}
    json.dump(m, open(mp, "w"))


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


def rewrite(tmp, rows_fn=None, ce_fn=None, rehash=True):
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
    if rehash:  # the manifest keeps the EXPECTED pilot; only the artifact hashes follow the edit, so each logic check is tested on its own
        write_manifest(tmp)


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


def clone_question(rows, ces, src="q0", dst="q99"):
    """Add a copy of question `src` under a new id at every level (rows and CE)."""
    return (rows + [dict(r, qid=dst) for r in rows if r["qid"] == src], ces + [dict(c, qid=dst) for c in ces if c["qid"] == src])


class PilotManifest(unittest.TestCase):
    """Evidence must match the committed pilot manifest exactly (question ids, clusters, levels, artifact hashes); the reviewer's attacks."""

    def run_case(self, rows_fn=None, ce_fn=None, rehash=True, drop_manifest=False):
        with tempfile.TemporaryDirectory() as t:
            make(t, True)
            rewrite(t, rows_fn, ce_fn, rehash)
            if drop_manifest:
                os.remove(os.path.join(t, "pilot_manifest.json"))
            return sc.score(t)

    def test_manifest_is_required(self):
        self.assertEqual(self.run_case(drop_manifest=True)["recommendation"]["category"], WITHHELD)

    def test_complete_fixture_records_the_manifest_hash_and_keeps(self):
        r = self.run_case()
        self.assertEqual(r["recommendation"]["category"], "KEEP")
        self.assertEqual(len(r["manifest_sha256"]), 64)
        self.assertEqual(set(r["artifact_sha256"]), {"rows.jsonl.gz", "ce.jsonl"})

    def test_removing_a_whole_question_at_every_level_withholds(self):
        r = self.run_case(rows_fn=lambda rows: [x for x in rows if x["qid"] != "q0"], ce_fn=lambda ce: [x for x in ce if x["qid"] != "q0"])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_adding_a_cloned_question_at_every_level_withholds(self):
        with tempfile.TemporaryDirectory() as t:
            make(t, True)
            rows = [json.loads(l) for l in gzip.open(os.path.join(t, "rows.jsonl.gz"), "rt")]
            ces = [json.loads(l) for l in open(os.path.join(t, "ce.jsonl"))]
            rows2, ces2 = clone_question(rows, ces)
            rewrite(t, rows_fn=lambda _: rows2, ce_fn=lambda _: ces2)
            self.assertEqual(sc.score(t)["recommendation"]["category"], WITHHELD)

    def test_replacing_a_question_id_keeping_the_count_withholds(self):
        r = self.run_case(rows_fn=lambda rows: [dict(x, qid="qX") if x["qid"] == "q3" else x for x in rows],
                          ce_fn=lambda ce: [dict(x, qid="qX") if x["qid"] == "q3" else x for x in ce])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_collapsing_every_cluster_to_one_withholds(self):
        r = self.run_case(rows_fn=lambda rows: [dict(x, cluster="one") for x in rows])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_swapping_two_questions_clusters_keeps_the_count_but_withholds(self):
        swap = {"c1": "c2", "c2": "c1"}
        r = self.run_case(rows_fn=lambda rows: [dict(x, cluster=swap.get(x["cluster"], x["cluster"])) for x in rows])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_a_cluster_that_differs_between_levels_withholds(self):
        r = self.run_case(rows_fn=lambda rows: [dict(x, cluster="c7x") if (x["qid"] == "q7" and x["level"] == "H3") else x for x in rows])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_consistently_swapping_h0_and_h3_labels_is_caught_by_the_artifact_hash(self):
        flip = {"H0": "H3", "H3": "H0"}
        r = self.run_case(rows_fn=lambda rows: [dict(x, level=flip.get(x["level"], x["level"])) for x in rows],
                          ce_fn=lambda ce: [dict(x, level=flip.get(x["level"], x["level"])) for x in ce], rehash=False)
        self.assertEqual(r["recommendation"]["category"], WITHHELD)
        self.assertTrue(any("hash" in p for p in r["evidence"]["problems"]))

    def test_swapped_h0_and_h3_labels_are_caught_even_if_the_hashes_are_rewritten(self):
        flip = {"H0": "H3", "H3": "H0"}
        r = self.run_case(rows_fn=lambda rows: [dict(x, level=flip.get(x["level"], x["level"])) for x in rows],
                          ce_fn=lambda ce: [dict(x, level=flip.get(x["level"], x["level"])) for x in ce], rehash=True)
        self.assertEqual(r["recommendation"]["category"], WITHHELD)
        self.assertTrue(any("n_level_sessions" in p for p in r["evidence"]["problems"]))

    def test_any_edit_of_an_artifact_after_stamping_is_caught_by_the_hash(self):
        r = self.run_case(rows_fn=lambda rows: [dict(x, extra="x") for x in rows], rehash=False)
        self.assertEqual(r["recommendation"]["category"], WITHHELD)


class MalformedEvidence(unittest.TestCase):
    """Malformed evidence gives WITHHELD, never a traceback and never KEEP."""

    def run_case(self, rows_fn=None, ce_fn=None):
        with tempfile.TemporaryDirectory() as t:
            make(t, True)
            rewrite(t, rows_fn, ce_fn)
            return sc.score(t)

    def test_missing_ce_exact_withholds_without_a_traceback(self):
        r = self.run_case(ce_fn=lambda ce: [{k: v for k, v in c.items() if k != "ce_exact"} if i == 0 else c for i, c in enumerate(ce)])
        self.assertEqual(r["recommendation"]["category"], WITHHELD)

    def test_nan_round_id_withholds_without_a_traceback(self):
        def rf(rows):
            rows = [dict(x) for x in rows]
            rows[0]["exact"] = dict(rows[0]["exact"], bm25=[float("nan")] + rows[0]["exact"]["bm25"][1:])
            return rows
        self.assertEqual(self.run_case(rows_fn=rf).get("recommendation", {}).get("category"), WITHHELD)

    def test_non_string_gold_withholds(self):
        self.assertEqual(self.run_case(rows_fn=lambda rows: [dict(x, gold=[1]) if i == 0 else x for i, x in enumerate(rows)])["recommendation"]["category"], WITHHELD)

    def test_nan_ce_score_withholds(self):
        def cf(ce):
            ce = [dict(c) for c in ce]
            ce[0]["scores"] = dict(ce[0]["scores"], **{ce[0]["ce_exact"][0]: float("nan")})
            return ce
        self.assertEqual(self.run_case(ce_fn=cf)["recommendation"]["category"], WITHHELD)

    def test_non_numeric_ce_score_withholds(self):
        def cf(ce):
            ce = [dict(c) for c in ce]
            ce[0]["scores"] = dict(ce[0]["scores"], **{ce[0]["ce_exact"][0]: "high"})
            return ce
        self.assertEqual(self.run_case(ce_fn=cf)["recommendation"]["category"], WITHHELD)

    def test_ce_order_that_disagrees_with_its_scores_withholds(self):
        def cf(ce):
            ce = [dict(c) for c in ce]
            ce[0]["ce_exact"] = list(reversed(ce[0]["ce_exact"]))
            return ce
        self.assertEqual(self.run_case(ce_fn=cf)["recommendation"]["category"], WITHHELD)

    def test_ce_scores_missing_or_for_other_rounds_withhold(self):
        def cf(ce):
            ce = [dict(c) for c in ce]
            ce[0]["scores"] = {k: v for k, v in list(ce[0]["scores"].items())[1:]}
            return ce
        self.assertEqual(self.run_case(ce_fn=cf)["recommendation"]["category"], WITHHELD)


if __name__ == "__main__":
    unittest.main()
