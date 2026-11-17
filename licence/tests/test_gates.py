"""Gate tests for review-2 findings F1 to F4 (THRESHOLDS.md addendum C). No node needed.
Run from the repo root: PYTHONPATH=licence:. python3 licence/tests/test_gates.py
Every test is written to FAIL on cc45153 (the reviewed commit) and pass after the fix."""
import copy
import gzip
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIC = os.path.dirname(HERE)
sys.path.insert(0, LIC)
sys.path.insert(0, os.path.dirname(LIC))
import exp2  # noqa: E402
import exp3  # noqa: E402
import recompute  # noqa: E402
import summarize  # noqa: E402

RES = os.path.join(LIC, "results")


def load(name):
    return json.load(open(os.path.join(RES, name)))


# ---------------------------------------------------------------- F1
class RawEvidenceTests(unittest.TestCase):
    def test_manifest_matches_every_file(self):
        import hashlib
        lines = [l.split() for l in open(os.path.join(RES, "MANIFEST.sha256")) if l.strip()]
        self.assertEqual(len(lines), 31)
        for digest, name in lines:
            p = os.path.join(RES, name)
            self.assertTrue(os.path.exists(p), name)
            self.assertEqual(hashlib.sha256(open(p, "rb").read()).hexdigest(), digest, name)

    def test_recompute_from_committed_rows_reproduces_recompute_all(self):
        files = sorted(f for f in os.listdir(RES) if (f.startswith("down_") or f.startswith("up_")) and f.endswith(".json"))
        got = [recompute.one(os.path.join(RES, f)) for f in files]
        self.assertEqual(json.loads(json.dumps(got, sort_keys=True)), json.load(open(os.path.join(RES, "recompute_all.json"))))


# ---------------------------------------------------------------- F2
class T5Tests(unittest.TestCase):
    def test_down_control_failing_for_unrelated_reasons_does_not_pass(self):
        R = load("down_1.json")
        c = R["exp2"]["T5_control_after_1s"]
        c["licence_failures"], c["other_failures"] = 0, c["n"]   # every failure now unrelated to licensing
        self.assertAlmostEqual(c["fail_fraction"], 1.0)
        self.assertFalse(summarize.exp2_checks(R, "down")["T5_pass"])

    def test_down_control_with_licence_failures_still_passes(self):
        self.assertTrue(summarize.exp2_checks(load("down_1.json"), "down")["T5_pass"])

    def test_down_control_with_no_requests_does_not_pass(self):
        R = load("down_1.json")
        R["exp2"]["T5_control_after_1s"].update({"n": 0, "licence_failures": 0, "fail_fraction": 0.0})
        self.assertFalse(summarize.exp2_checks(R, "down")["T5_pass"])

    def test_up_control_before_trial_failing_for_unrelated_reasons_does_not_pass(self):
        R = load("up_1.json")
        pre = R["exp2"]["T5_control_before_start_trial"]
        pre["licence_failures"] = 0   # all `failures` unrelated to licensing
        self.assertFalse(summarize.exp2_checks(R, "up")["T5_pass"])

    def test_up_legacy_record_without_licence_count_fails_closed(self):
        R = load("up_1.json")
        self.assertNotIn("licence_failures", R["exp2"]["T5_control_before_start_trial"])
        self.assertFalse(summarize.exp2_checks(R, "up")["T5_pass"])

    def test_up_legacy_record_is_backfilled_from_rows(self):
        R = summarize.load(RES, "up")[0]
        self.assertGreater(R["exp2"]["T5_control_before_start_trial"]["licence_failures"], 0)
        self.assertTrue(summarize.exp2_checks(R, "up")["T5_pass"])


class SemFailureClassTests(unittest.TestCase):
    def test_licence_signature_is_loud(self):
        e = {"type": "security_exception", "reason": "current license is non-compliant for [inference]", "root": ""}
        self.assertEqual(exp2.classify_sem_failure(403, e), "licence")

    def test_unrelated_403_is_not_loud(self):
        e = {"type": "security_exception", "reason": "action [indices:data/write/index] is unauthorized for user [x]", "root": ""}
        self.assertEqual(exp2.classify_sem_failure(403, e), "other")

    def test_missing_index_404_is_not_loud(self):
        e = {"type": "index_not_found_exception", "reason": "no such index [lic-sem2]", "root": ""}
        self.assertEqual(exp2.classify_sem_failure(404, e), "other")

    def test_missing_endpoint_404_is_loud(self):
        e = {"type": "resource_not_found_exception", "reason": "Inference endpoint not found [lic-stub]", "root": ""}
        self.assertEqual(exp2.classify_sem_failure(404, e), "missing_endpoint")

    def test_real_recorded_404_bodies_are_missing_endpoint(self):
        # bodies copied from committed results (down_1.json exp1_basic0)
        for reason in ("Inference endpoint not found [lic-stub]", "Inference id [lic-stub] not found for field [body]"):
            e = {"type": "resource_not_found_exception", "reason": reason, "root": reason, "feature": None}
            self.assertEqual(exp2.classify_sem_failure(404, e), "missing_endpoint")

    def test_transport_failure_is_not_loud(self):
        self.assertEqual(exp2.classify_sem_failure(0, None), "other")


# ---------------------------------------------------------------- F3
def workload_with(rows, rrf=None, lin=None, legs=None):
    w = exp2.Workload.__new__(exp2.Workload)
    w.rows, w.queries, w.ttl, w.seed = rows, [("q", [0.0])] * 3, 5.0, 1
    w.oracle_rrf, w.oracle_lin, w.oracle_legs = rrf or {}, lin or {}, legs or {}
    return w


def row(t, arm, qi, fus, ids, ok=1, err=""):
    return [t, t + 0.01, arm, qi, ok, fus, ids, err]


class T1Tests(unittest.TestCase):
    IDS = ["d%d" % i for i in range(10)]

    def test_backend_answer_without_oracle_is_not_examined(self):
        w = workload_with([row(1.0, "auto", 0, "backend", self.IDS)], rrf={})
        out = w.analyse(0.5, 0.6, "down")
        self.assertEqual(out["T1_examined"], 0)
        self.assertEqual(out["T1_backend_without_oracle"], 1)

    def test_backend_answer_with_oracle_is_examined(self):
        w = workload_with([row(1.0, "auto", 0, "backend", self.IDS)], rrf={0: self.IDS}, legs={0: (self.IDS, self.IDS[::-1])})
        out = w.analyse(0.5, 0.6, "down")
        self.assertEqual((out["T1_examined"], out["T1_mismatches"], out["T1_backend_without_oracle"]), (1, 0, 0))

    def test_gate_requires_complete_oracle_coverage(self):
        R = load("down_1.json")
        R["exp2"]["T1_backend_without_oracle"] = R["exp2"]["T1_examined"]
        self.assertFalse(summarize.exp2_checks(R, "down")["T1_pass"])

    def test_gate_fails_closed_when_counter_absent(self):
        R = load("down_1.json")
        R["exp2"].pop("T1_backend_without_oracle", None)
        self.assertFalse(summarize.exp2_checks(R, "down")["T1_pass"])


# ---------------------------------------------------------------- F4
class FakeES:
    """Minimal in-memory source index with scroll paging; fail_after = serve this many scroll pages then answer fail_status."""

    def __init__(self, n=5, page=2, fail_after=None, fail_status=500, count=None, shard_failed=0):
        self.docs = [{"_id": str(i), "_source": {"title": "t%d" % i, "tag": "a", "v": [float(i)] * exp3.DIM}} for i in range(n)]
        self.page, self.fail_after, self.fail_status, self.pos, self.pages, self.shard_failed = page, fail_after, fail_status, 0, 0, shard_failed
        self.count_value = n if count is None else count
        self.bulked = []

    def _page(self):
        self.pages += 1
        if self.fail_after is not None and self.pages > self.fail_after:
            return self.fail_status, {"error": {"type": "search_phase_execution_exception"}}, 1
        hits = self.docs[self.pos:self.pos + self.page]
        self.pos += self.page
        return 200, {"_scroll_id": "sid%d" % self.pages, "timed_out": False, "_shards": {"total": 1, "successful": 1 - self.shard_failed, "failed": self.shard_failed}, "hits": {"hits": hits}}, 1

    def req(self, method, path, body=None, ndjson=None, timeout=60, **kw):
        if method == "GET" and path.endswith("/_count"):
            return 200, {"count": self.count_value}, 1
        if "_search/scroll" in path and method == "POST":
            return self._page()
        if method == "POST" and "_search?scroll" in path:
            return self._page()
        return 200, {"acknowledged": True}, 1

    def bulk(self, ix, items, refresh="false", op="index"):
        self.bulked.extend(k for k, _ in items)
        return 200, {"failed": 0}


class MigrationCompletenessTests(unittest.TestCase):
    def setUp(self):
        self._req, self._bulk = exp3.req, exp3.bulk

    def tearDown(self):
        exp3.req, exp3.bulk = self._req, self._bulk

    def use(self, es):
        exp3.req, exp3.bulk = es.req, es.bulk

    def test_non200_scroll_after_first_page_raises_and_writes_nothing(self):
        es = FakeES(n=5, page=2, fail_after=1)
        self.use(es)
        with self.assertRaises(exp3.ScrollError):
            exp3.migrate_vectors("src", "dst")
        self.assertEqual(es.bulked, [])

    def test_shard_failure_raises(self):
        es = FakeES(n=4, page=2, shard_failed=1)
        self.use(es)
        with self.assertRaises(exp3.ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_independent_count_mismatch_raises(self):
        es = FakeES(n=4, page=2, count=5)   # the source says 5 documents, the scroll yields 4
        self.use(es)
        with self.assertRaises(exp3.ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_complete_scroll_migrates_every_document(self):
        es = FakeES(n=5, page=2)
        self.use(es)
        docs = exp3.migrate_vectors("src", "dst")
        self.assertEqual(sorted(docs), ["0", "1", "2", "3", "4"])
        self.assertEqual(sorted(es.bulked), ["0", "1", "2", "3", "4"])

    def test_semantic_non200_scroll_raises(self):
        es = FakeES(n=5, page=2, fail_after=1)
        self.use(es)
        with self.assertRaises(exp3.ScrollError):
            exp3.migrate_semantic("src", "dst")

    def test_check_vectors_non200_scroll_of_destination_raises(self):
        es = FakeES(n=5, page=2, fail_after=1)
        self.use(es)
        src = {str(i): {"v": [float(i)] * exp3.DIM} for i in range(5)}
        with self.assertRaises(exp3.ScrollError):
            exp3.check_vectors(src, "dst")


class MigrationVerdictTests(unittest.TestCase):
    def test_recorded_lives_have_independent_source_counts_and_pass(self):
        for n in (1, 2, 3, 4, 5, 6, 7):
            R = load("down_%d.json" % n)
            chk = summarize.exp3_checks(R)["migration_checks"]
            self.assertTrue(chk["bbq_disk_to_hnsw"] and chk["bbq_disk_to_bbq_hnsw"] and chk["semantic_to_text_vector"], n)
            self.assertTrue(chk["source_count_independent"], n)

    def test_source_count_not_matching_independent_count_fails(self):
        R = load("down_1.json")
        R["exp3"]["migration"]["bbq_disk_to_hnsw"]["src_count"] = 298
        R["exp3"]["migration"]["bbq_disk_to_hnsw"]["dst_count"] = 298   # a partial copy that agrees with itself
        chk = summarize.exp3_checks(R)["migration_checks"]
        self.assertFalse(chk["bbq_disk_to_hnsw"])
        self.assertFalse(chk["source_count_independent"])


if __name__ == "__main__":
    unittest.main()
