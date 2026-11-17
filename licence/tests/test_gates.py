"""Gate tests for review-2 findings F1 to F4 and delta review-1 N1 to N2 (THRESHOLDS.md addenda C and D). No node needed.
Run from the repo root: PYTHONPATH=licence:. python3 licence/tests/test_gates.py

Measured against the old code (35 tests; the same file run on a detached checkout of each commit with the committed rows copied in):
- cc45153 (reviewed commit): 24 of 35 fail, 11 pass. Failing by behaviour (old code runs and answers wrongly, through the shims below): the T5 unrelated-failure tests
  (2) and legacy-up fail-closed, the T1 oracle tests (3), the migration scroll/refresh/count/duplicate/timed-out tests (non-200, shard failure, count mismatch,
  semantic, refresh status, refresh shards, count shards, duplicate, timed out), the stale-scroll-id fake (test_complete_scroll_migrates_every_document: the old
  code reuses the first scroll id), the unrelated-403 and missing-index SemFailure tests (against a RECONSTRUCTION of the old rule), the T4 UNVERIFIED test,
  test_source_count_not_matching_independent_count_fails. Failing only because a new output key or function is absent: test_up_legacy_record_is_backfilled_from_rows,
  test_recorded_lives_have_independent_source_counts_and_pass, test_classified_record_with_other_failures_fails, and test_manifest_matches_every_file (stale manifest).
  PASS on cc45153, so NOT failing-first: test_check_vectors_non200_scroll_of_destination_raises (the shim accepts any exception, so it is vacuous there),
  test_recompute_from_committed_rows_reproduces_recompute_all, the four SemFailure tests that agree with the old rule (licence signature, missing endpoint,
  real recorded 404 bodies, transport failure), test_backend_answer_with_oracle_is_examined, test_classified_clean_record_passes,
  test_no_semantic_failures_needs_no_classification, test_down_control_with_licence_failures_still_passes, test_down_control_with_no_requests_does_not_pass.
- fad672c (delta-reviewed commit): 5 of 35 fail, all N1/N2 tests: test_every_recorded_life_is_unverified_not_pass, test_classified_record_with_other_failures_fails
  (T4_status key absent), test_failed_refresh_status_raises, test_refresh_shard_failure_raises, test_count_shard_failure_raises.
Added after final-gate reviews F2 and F3 (55 tests now): the bulk-wire tests (10 fail on 207136e, all by 'not raised') and MatrixFixtureTests (on 4eb4378: 8 fail by behaviour,
- failed creation, failed write, partial write, empty index, two full-matrix aborts, empty vector cell labelled AVAILABLE, exp3 semantic-only fixture; test_good_fixture_is_accepted passes there).
The fake validates the scroll id it is given (a stale id is rejected). Timed-out and duplicate-id cases are covered. Nothing here touches a live node.
"""
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
import lib  # noqa: E402
import matrix  # noqa: E402
import exp3  # noqa: E402
import recompute  # noqa: E402
import summarize  # noqa: E402

RES = os.path.join(LIC, "results")
# Shims so that old code fails by behaviour, not by a missing name. ScrollError: old migrations raise nothing, so assertRaises(Exception) fails there.
ScrollError = getattr(exp3, "ScrollError", Exception)
BulkError = getattr(lib, "BulkError", Exception)
# FixtureError: on code without it, a class that is never raised, so a missing abort shows as an assertion failure, not as a vacuous pass.
FixtureError = getattr(matrix, "FixtureError", type("NeverRaised", (Exception,), {}))


def _old_rule(code, e):   # reconstruction of the cc45153 rule: any 403/404 or the licence text counted as loud
    return "licence" if e and ("non-compliant" in json.dumps(e) or code in (404, 403)) else "other"


classify = getattr(exp2, "classify_sem_failure", _old_rule)
WANT_MISSING = "missing_endpoint" if hasattr(exp2, "classify_sem_failure") else "licence"


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
        self.assertEqual(classify(403, e), "licence")

    def test_unrelated_403_is_not_loud(self):
        e = {"type": "security_exception", "reason": "action [indices:data/write/index] is unauthorized for user [x]", "root": ""}
        self.assertEqual(classify(403, e), "other")

    def test_missing_index_404_is_not_loud(self):
        e = {"type": "index_not_found_exception", "reason": "no such index [lic-sem2]", "root": ""}
        self.assertEqual(classify(404, e), "other")

    def test_missing_endpoint_404_is_loud(self):
        e = {"type": "resource_not_found_exception", "reason": "Inference endpoint not found [lic-stub]", "root": ""}
        self.assertEqual(classify(404, e), WANT_MISSING)

    def test_real_recorded_404_bodies_are_missing_endpoint(self):
        # bodies copied from committed results (down_1.json exp1_basic0)
        for reason in ("Inference endpoint not found [lic-stub]", "Inference id [lic-stub] not found for field [body]"):
            e = {"type": "resource_not_found_exception", "reason": reason, "root": reason, "feature": None}
            self.assertEqual(classify(404, e), WANT_MISSING)

    def test_transport_failure_is_not_loud(self):
        self.assertEqual(classify(0, None), "other")


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

    def __init__(self, n=5, page=2, fail_after=None, fail_status=500, count=None, shard_failed=0, refresh_status=200, refresh_failed=0, count_failed=0,
                 timed_out=False, dup=False, bulk_mode=None, dst_refresh_status=200, dst_refresh_failed=0):
        self.bulk_mode, self.dst_refresh_status, self.dst_refresh_failed = bulk_mode, dst_refresh_status, dst_refresh_failed
        self.refresh_status, self.refresh_failed, self.count_failed, self.timed_out, self.dup, self.last_sid = refresh_status, refresh_failed, count_failed, timed_out, dup, None
        self.docs = [{"_id": str(i), "_source": {"title": "t%d" % i, "tag": "a", "body": "b%d" % i, "v": [float(i)] * exp3.DIM,
                                                   "_inference_fields": {"body": {"inference": {"chunks": {"body": [{"embeddings": [0.1] * 8}]}}}}}} for i in range(n)]
        self.page, self.fail_after, self.fail_status, self.pos, self.pages, self.shard_failed = page, fail_after, fail_status, 0, 0, shard_failed
        self.count_value = n if count is None else count
        self.bulked = []

    def _page(self):
        self.pages += 1
        if self.fail_after is not None and self.pages > self.fail_after:
            return self.fail_status, {"error": {"type": "search_phase_execution_exception"}}, 1
        hits = self.docs[self.pos:self.pos + self.page]
        if self.dup and self.pages == 2 and hits:
            hits = [self.docs[0]]
        self.pos += self.page
        self.last_sid = "sid%d" % self.pages
        return 200, {"_scroll_id": self.last_sid, "timed_out": self.timed_out and self.pages == 2, "_shards": {"total": 1, "successful": 1 - self.shard_failed, "failed": self.shard_failed}, "hits": {"hits": hits}}, 1

    def req(self, method, path, body=None, ndjson=None, timeout=60, **kw):
        if method == "GET" and path.endswith("/_count"):
            return 200, {"count": self.count_value, "_shards": {"total": 1, "successful": 1 - self.count_failed, "failed": self.count_failed}}, 1
        if method == "POST" and path.startswith("/_bulk"):
            return self._bulk(ndjson)
        if method == "POST" and path.endswith("/_refresh") and "/dst/" in path + "/":
            return self.dst_refresh_status, {"_shards": {"total": 1, "successful": 1 - self.dst_refresh_failed, "failed": self.dst_refresh_failed}}, 1
        if method == "POST" and path.endswith("/_refresh") and "/src/" in path + "/":
            return self.refresh_status, {"_shards": {"total": 1, "successful": 1 - self.refresh_failed, "failed": self.refresh_failed}}, 1
        if "_search/scroll" in path and method == "POST":
            if self.last_sid is not None and (body or {}).get("scroll_id") != self.last_sid:
                return 404, {"error": {"type": "search_context_missing_exception"}}, 1   # a stale scroll id is rejected, as Elasticsearch does
            return self._page()
        if method == "POST" and "_search?scroll" in path:
            return self._page()
        return 200, {"acknowledged": True}, 1

    def _bulk(self, ndjson):
        """Real wire format in, Elasticsearch-shaped answer out; failure modes injected by bulk_mode."""
        ids = [list(json.loads(l).values())[0]["_id"] for l in ndjson.splitlines()[::2]]
        m = self.bulk_mode
        if m == "transport":
            return 0, {"_raw": ""}, 1
        if m == "503":
            return 503, {"error": {"type": "es_rejected_execution_exception", "reason": "queue full"}, "status": 503}, 1
        if m == "noitems":
            return 200, {"took": 1, "errors": False}, 1
        if m == "malformed":
            return 200, {"_raw": "<html>"}, 1
        items = [{"index": {"_id": i, "status": 201}} for i in ids]
        if m == "truncated":
            items = items[:-1]
        if m == "item_error":
            items[0] = {"index": {"_id": ids[0], "status": 403, "error": {"type": "security_exception", "reason": "non-compliant"}}}
        self.bulked.extend(i for it in items for i in [list(it.values())[0]["_id"]] if list(it.values())[0]["status"] < 300)
        return 200, {"took": 1, "errors": m == "item_error", "items": items}, 1


class MigrationCompletenessTests(unittest.TestCase):
    def setUp(self):
        self._req, self._lreq = exp3.req, lib.req

    def tearDown(self):
        exp3.req, lib.req = self._req, self._lreq

    def use(self, es):
        exp3.req, lib.req = es.req, es.req   # the real lib.bulk and bulk_strict run against the fake wire

    def test_non200_scroll_after_first_page_raises_and_writes_nothing(self):
        es = FakeES(n=5, page=2, fail_after=1)
        self.use(es)
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")
        self.assertEqual(es.bulked, [])

    def test_shard_failure_raises(self):
        es = FakeES(n=4, page=2, shard_failed=1)
        self.use(es)
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_independent_count_mismatch_raises(self):
        es = FakeES(n=4, page=2, count=5)   # the source says 5 documents, the scroll yields 4
        self.use(es)
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def _bulk_case(self, mode, fn="vec", **kw):
        es = FakeES(n=5, page=2, bulk_mode=mode, **kw)
        self.use(es)
        with self.assertRaises((ScrollError, BulkError)):
            (exp3.migrate_vectors if fn == "vec" else exp3.migrate_semantic)("src", "dst")

    def test_bulk_transport_failure_raises(self):
        self._bulk_case("transport")

    def test_bulk_http_503_with_error_body_raises(self):
        self._bulk_case("503")

    def test_bulk_response_without_items_raises(self):
        self._bulk_case("noitems")

    def test_bulk_malformed_response_raises(self):
        self._bulk_case("malformed")

    def test_bulk_truncated_items_raises(self):
        self._bulk_case("truncated")

    def test_bulk_item_error_raises(self):
        self._bulk_case("item_error")

    def test_semantic_bulk_503_raises(self):
        self._bulk_case("503", fn="sem")

    def test_semantic_bulk_truncated_raises(self):
        self._bulk_case("truncated", fn="sem")

    def test_destination_refresh_failure_raises(self):
        self._bulk_case(None, dst_refresh_status=503)

    def test_destination_refresh_shard_failure_raises(self):
        self._bulk_case(None, dst_refresh_failed=1)

    def test_recording_bulk_counts_unanswered_operations_as_failed(self):
        for mode in ("transport", "503", "noitems", "truncated", "item_error"):
            es = FakeES(n=5, page=2, bulk_mode=mode)
            self.use(es)
            c, s = lib.bulk("dst", [(str(i), {"v": [0.0]}) for i in range(3)])
            self.assertGreater(s["failed"], 0, mode)
            self.assertIsNotNone(s["first_error"], mode)

    def test_failed_refresh_status_raises(self):
        self.use(FakeES(n=4, page=2, refresh_status=503))
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_refresh_shard_failure_raises(self):
        self.use(FakeES(n=4, page=2, refresh_failed=1))
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_count_shard_failure_raises(self):
        self.use(FakeES(n=4, page=2, count_failed=1))
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_timed_out_page_raises(self):   # added coverage: also passes on the old code
        self.use(FakeES(n=6, page=2, timed_out=True))
        with self.assertRaises(ScrollError):
            exp3.migrate_vectors("src", "dst")

    def test_duplicate_id_raises(self):   # added coverage: new code only
        self.use(FakeES(n=6, page=2, dup=True))
        with self.assertRaises(ScrollError):
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
        with self.assertRaises(ScrollError):
            exp3.migrate_semantic("src", "dst")

    def test_check_vectors_non200_scroll_of_destination_raises(self):
        es = FakeES(n=5, page=2, fail_after=1)
        self.use(es)
        src = {str(i): {"v": [float(i)] * exp3.DIM} for i in range(5)}
        with self.assertRaises(ScrollError):
            exp3.check_vectors(src, "dst")


class T4Tests(unittest.TestCase):
    def test_every_recorded_life_is_unverified_not_pass(self):
        for f in sorted(os.listdir(RES)):
            if (f.startswith("down_") or f.startswith("up_")) and f.endswith(".json"):
                R = load(f)
                d = "down" if f.startswith("down") else "up"
                r = summarize.exp2_checks(R, d)
                self.assertGreater(R["exp2"]["writes"]["sem_failed"], 0, f)
                self.assertNotIn("sem_failed_other", R["exp2"]["writes"], f)
                self.assertFalse(r["T4_pass"], f)
                self.assertEqual(r["T4_status"], "UNVERIFIED", f)
                self.assertTrue(r["T4_legacy_pass"], f)   # the old-rule result is kept, separately named

    def test_classified_clean_record_passes(self):
        R = load("down_1.json")
        R["exp2"]["writes"]["sem_failed_other"] = 0
        self.assertTrue(summarize.exp2_checks(R, "down")["T4_pass"])

    def test_classified_record_with_other_failures_fails(self):
        R = load("down_1.json")
        w = R["exp2"]["writes"]
        w["sem_failed_other"], w["sem_failed_loud_licence_or_missing"] = 3, w["sem_failed"] - 3
        self.assertEqual(summarize.exp2_checks(R, "down")["T4_status"], "FAIL")

    def test_no_semantic_failures_needs_no_classification(self):
        R = load("down_1.json")
        w = R["exp2"]["writes"]
        w["sem_failed"] = w["sem_failed_loud_licence_or_missing"] = 0
        self.assertTrue(summarize.exp2_checks(R, "down")["T4_pass"])


class MatrixFake:
    """In-memory Elasticsearch for fixture tests. bulk_mode (limited to indices containing `only`): 503 writes nothing; truncated writes all but the
    last and answers n-1 items; empty answers success for every item but writes nothing. create_status applies to PUT /<index>."""

    def __init__(self, bulk_mode=None, only="", create_status=200):
        self.bulk_mode, self.only, self.create_status, self.docs = bulk_mode, only, create_status, {}

    def req(self, method, path, body=None, ndjson=None, timeout=60, **kw):
        ok_sh = {"_shards": {"total": 1, "successful": 1, "failed": 0}}
        p = path.split("?")[0]
        parts = [x for x in p.split("/") if x]
        if method == "DELETE":
            self.docs.pop(parts[0], None) if parts else None
            return 200, {"acknowledged": True}, 1
        if method == "POST" and p == "/_bulk":
            lines = ndjson.splitlines()
            ops = [(list(json.loads(l).values())[0], json.loads(d)) for l, d in zip(lines[::2], lines[1::2])]
            idx = ops[0][0]["_index"]
            m = self.bulk_mode if self.only in idx else None
            if m == "503":
                return 503, {"error": {"type": "es_rejected_execution_exception"}, "status": 503}, 1
            kept = ops[:-1] if m == "truncated" else ops
            if m != "empty":
                self.docs.setdefault(idx, {}).update({o["_id"]: s for o, s in kept})
            return 200, {"errors": False, "items": [{"index": {"_id": o["_id"], "status": 201}} for o, _ in kept]}, 1
        if method == "PUT" and len(parts) == 1:
            if self.create_status != 200:
                return self.create_status, {"error": {"type": "x"}}, 1
            self.docs.setdefault(parts[0], {})
            return 200, {"acknowledged": True}, 1
        if len(parts) == 2 and parts[1] == "_refresh":
            return 200, dict(ok_sh), 1
        if len(parts) == 2 and parts[1] == "_count":
            return 200, dict({"count": len(self.docs.get(parts[0], {}))}, **ok_sh), 1
        if len(parts) == 3 and parts[1] == "_doc" and method == "GET":
            d = self.docs.get(parts[0], {}).get(parts[2])
            return (200, {"found": True, "_source": d}, 1) if d is not None else (404, {"found": False}, 1)
        return 200, {"hits": {"hits": []}, "acknowledged": True, "endpoints": []}, 1


class MatrixFixtureTests(unittest.TestCase):
    def setUp(self):
        self._r = (matrix.req, lib.req, exp3.req)
        self._m = (matrix.ensure_dls, matrix.dls_search, matrix.DLS_PW)
        matrix.ensure_dls = lambda: {"role": {"class": "SKIP", "status": None}}
        matrix.dls_search = lambda: {"class": "SKIP", "status": None}
        matrix.DLS_PW = "/nonexistent/dls_password"

    def tearDown(self):
        matrix.req, lib.req, exp3.req = self._r
        matrix.ensure_dls, matrix.dls_search, matrix.DLS_PW = self._m

    def use(self, fake):
        matrix.req = lib.req = exp3.req = fake.req
        return fake

    def aborts(self, fn):
        try:
            out = fn()
        except FixtureError:
            return True
        self.fail("returned normally instead of aborting: %s" % json.dumps(out, default=str)[:200])

    def test_good_fixture_is_accepted(self):
        f = self.use(MatrixFake())
        matrix.make_base()
        self.assertEqual(len(f.docs[matrix.BASE_IDX]), len(matrix.DOCS))

    def test_failed_creation_aborts(self):
        self.use(MatrixFake(create_status=500))
        self.aborts(matrix.make_base)

    def test_failed_fixture_write_aborts(self):
        self.use(MatrixFake(bulk_mode="503"))
        self.aborts(matrix.make_base)

    def test_partial_fixture_write_aborts(self):
        self.use(MatrixFake(bulk_mode="truncated"))
        self.aborts(matrix.make_base)

    def test_empty_index_after_success_answers_aborts(self):
        self.use(MatrixFake(bulk_mode="empty"))
        self.aborts(matrix.make_base)

    def test_full_matrix_aborts_on_failed_base_write(self):
        self.use(MatrixFake(bulk_mode="503", only="base"))
        self.aborts(lambda: matrix.run("t"))

    def test_full_matrix_aborts_on_empty_force_merge_fixture(self):
        self.use(MatrixFake(bulk_mode="empty", only="-fm"))
        self.aborts(lambda: matrix.run("t"))

    def test_vector_cell_is_not_available_when_index_is_empty(self):
        self.use(MatrixFake(bulk_mode="empty", only="int8_hnsw"))
        T = matrix.run("t")
        self.assertNotEqual(T["vec_int8_hnsw"]["class"], "AVAILABLE")
        self.assertEqual(T["vec_bbq_hnsw"]["class"], "AVAILABLE")

    def test_exp3_semantic_only_fixture_with_failed_write_aborts(self):
        self.use(MatrixFake(bulk_mode="503"))
        with self.assertRaises((ScrollError, BulkError)):
            exp3.semonly_fixture()


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
