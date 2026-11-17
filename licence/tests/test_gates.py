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
Added after gate review 4 (86 tests now, with test_offline): MatrixInvariantTests enumerate every cell in matrix.CELLS and break each declared prerequisite in
each failure mode; the red run of this file against 90faede is licence/tests/red/90faede.txt (14 failures by behaviour or a missing table, 6 errors
for names that 90faede does not have: verdict, Cells, NOT_OBSERVED). MatrixVerdictTests are new-code only.
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
    """In-memory Elasticsearch for the matrix tests. Searches and ES|QL answer from the documents actually stored, so an empty or partial
    fixture shows up as it would on a node. inject: {target: mode}; targets are exact index names, "endpoint:<id>" (PUT/GET of an inference
    endpoint) or "user" (GET of the DLS user). Modes: 503, transport, truncated, empty, wrong_count, create_fail, licence. `fired` records each
    injection that was applied, so a scenario cannot pass because its injection never happened."""

    LICENCE = {"error": {"type": "security_exception", "reason": "current license is non-compliant for [x]"}, "status": 403}

    def __init__(self, inject=None, eps=("lic-stub",)):
        self.inject, self.fired, self.docs, self.aliases, self.eps = dict(inject or {}), [], {}, {}, set(eps)

    def mode(self, target, *kinds):
        m = self.inject.get(target)
        if m in kinds:
            self.fired.append((target, m))
            return m
        return None

    def fail(self, m):
        if m == "transport":
            return 0, {"_transport_error": "URLError"}, 1
        if m == "licence":
            return 403, copy.deepcopy(self.LICENCE), 1
        if m == "create_fail":
            return 500, {"error": {"type": "x", "reason": "boom"}, "status": 500}, 1
        return 503, {"error": {"type": "es_rejected_execution_exception", "reason": "busy"}, "status": 503}, 1

    def req(self, method, path, body=None, ndjson=None, timeout=60, **kw):
        ok_sh = {"total": 1, "successful": 1, "failed": 0}
        p = path.split("?")[0]
        parts = [x for x in p.split("/") if x]
        missing = (404, {"error": {"type": "index_not_found_exception", "reason": "no such index"}, "status": 404}, 1)
        if parts[:1] == ["_inference"]:
            if len(parts) == 1:
                return 200, {"endpoints": [{"inference_id": e} for e in sorted(self.eps)]}, 1
            ep = parts[-1]
            if method == "DELETE":
                self.eps.discard(ep)
                return 200, {"acknowledged": True}, 1
            if method in ("PUT", "GET"):
                m = self.mode("endpoint:" + ep, "503", "transport", "create_fail", "licence")
                if m:
                    return self.fail(m)
                if method == "PUT":
                    self.eps.add(ep)
            if ep not in self.eps:
                return 404, {"error": {"type": "resource_not_found_exception", "reason": "no endpoint"}, "status": 404}, 1
            return 200, {"text_embedding": [{"embedding": [0.1] * 8}]}, 1
        if parts[:1] == ["_security"]:
            if method == "GET":
                m = self.mode("user", "503", "transport", "create_fail")
                if m:
                    return (404, {}, 1) if m == "create_fail" else self.fail(m)
            return 200, {"created": True}, 1
        if p == "/_bulk":
            lines = ndjson.splitlines()
            ops = [(list(json.loads(l).values())[0], json.loads(d)) for l, d in zip(lines[::2], lines[1::2])]
            idx = ops[0][0]["_index"]
            m = self.mode("bulk:" + idx, "503", "transport", "truncated", "empty", "licence")
            if m in ("503", "transport"):
                return self.fail(m)
            if idx not in self.docs:
                return 200, {"errors": True, "items": [{"index": {"_id": o["_id"], "status": 404, "error": {"type": "index_not_found_exception"}}} for o, _ in ops]}, 1
            if m == "licence":
                return 200, {"errors": True, "items": [{"index": {"_id": o["_id"], "status": 403, "error": self.LICENCE["error"]}} for o, _ in ops]}, 1
            kept = ops[:-1] if m == "truncated" else ops
            if m != "empty":
                self.docs[idx].update({o["_id"]: s for o, s in kept})
            return 200, {"errors": False, "items": [{"index": {"_id": o["_id"], "status": 201}} for o, _ in kept]}, 1
        if p == "/_query":
            name = body["query"].split()[1]
            if name not in self.docs:
                return missing
            return 200, {"values": [[i] for i in sorted(self.docs[name])]}, 1
        if p in ("/_aliases", "/_msearch") or parts[:1] == ["_search"]:
            return 200, {"acknowledged": True, "responses": []}, 1
        if method == "DELETE" and len(parts) == 1:
            self.docs.pop(parts[0], None)
            return 200, {"acknowledged": True}, 1
        if method == "PUT" and len(parts) == 1:
            m = self.mode(parts[0], "create_fail", "transport", "503")
            if m:
                return self.fail(m)
            self.docs[parts[0]] = {}
            for a in ((body or {}).get("aliases") or {}):
                self.aliases[a] = parts[0]
            return 200, {"acknowledged": True}, 1
        idx = self.aliases.get(parts[0], parts[0])
        if idx not in self.docs:
            return missing
        op = parts[1] if len(parts) > 1 else ""
        if op == "_refresh":
            m = self.mode(idx, "503", "transport")
            return self.fail(m) if m else (200, {"_shards": dict(ok_sh)}, 1)
        if op == "_count":
            m = self.mode(idx, "wrong_count") or self.mode("doc:" + idx, "wrong_count")
            return 200, {"count": len(self.docs[idx]) + (1 if m else 0), "_shards": dict(ok_sh)}, 1
        if op == "_doc" and method == "PUT":
            m = self.mode("doc:" + idx, "503", "transport", "licence", "empty")
            if m in ("503", "transport", "licence"):
                return self.fail(m)
            if m != "empty":
                self.docs[idx][parts[2]] = body
            return 201, {"result": "created", "_id": parts[2]}, 1
        if op == "_doc":
            d = self.docs[idx].get(parts[2])
            return (200, {"found": True, "_source": d}, 1) if d is not None else (404, {"found": False}, 1)
        if op == "_search":
            ids = sorted(self.docs[idx])[:(body or {}).get("size", 10) or 10]
            return 200, {"hits": {"total": {"value": len(self.docs[idx])}, "hits": [{"_id": i} for i in ids]}, "_shards": dict(ok_sh)}, 1
        return 200, {"_shards": dict(ok_sh), "acknowledged": True}, 1


# Frozen copy of the registry for code that has none (90faede): the test then still runs the old matrix and fails by behaviour, not by a missing name.
_LEGACY_CELLS = {
    ("run", n): (pre, w) for n, pre, w in (
        ("bm25_match", ("base",), "hits"), ("dense_knn", ("base",), "hits"), ("client_side_fusion", ("base",), "hits"),
        ("retriever_rrf", ("base",), "hits"), ("retriever_linear", ("base",), "hits"), ("text_similarity_reranker_gate", ("base",), "hits"),
        ("vec_int8_hnsw", ("vec_int8_hnsw",), "hits"), ("knn_rescore_vector", ("vec_int8_hnsw",), "hits"), ("vec_bbq_hnsw", ("vec_bbq_hnsw",), "hits"),
        ("vec_bbq_disk", ("vec_bbq_disk",), "hits"), ("esql_match", ("base",), "values"), ("esql_fork", ("base",), "values"), ("esql_fuse", ("base",), "values"),
        ("inference_list", (), "status"), ("inference_create", (), "status"), ("inference_use_new", ("inference_ep_new",), "status"),
        ("inference_use_existing", ("stub_endpoint",), "status"), ("semantic_text_mapping", (), "status"), ("semantic_text_index", ("sem_mapping",), "status"),
        ("semantic_query", ("sem_mapping", "sem_doc"), "hits"), ("match_on_semantic_text", ("sem_mapping", "sem_doc"), "hits"),
        ("dls_role_create", (), "status"), ("dls_search_as_user", ("shared_base", "dls_principal"), "hits"),
        ("percolator_lexical", ("perc_index", "perc_doc"), "hits"), ("alias_require_alias", ("alias_index",), "status"),
        ("alias_must_exist", ("alias_index",), "status"), ("force_merge", ("fm",), "status"))}
_LEGACY_CELLS.update({("quick", n): (pre, w) for n, pre, w in (
    ("rrf", ("shared_base",), "hits"), ("linear", ("shared_base",), "hits"), ("bm25", ("shared_base",), "hits"), ("knn", ("shared_base",), "hits"),
    ("semantic_query", (), "hits"), ("inference_use", (), "status"), ("dls_search", ("shared_base", "dls_principal"), "hits"))})
_LEGACY_SCOPE = {"base": "run", "perc_index": "run", "alias_index": "run", "fm": "run"}
REGISTRY = {k: (tuple(v[0]), v[1]) for k, v in getattr(matrix, "CELLS", _LEGACY_CELLS).items()}
PREREQ_SCOPE = {p: v[0] for p, v in matrix.PREREQS.items()} if hasattr(matrix, "PREREQS") else \
    dict({p: "cell" for k, (pre, _w) in _LEGACY_CELLS.items() for p in pre}, **_LEGACY_SCOPE)

# How each prerequisite is broken: the injection target (label "t"; the bulk write of a fixture is "bulk:<index>", a single-document write "doc:<index>") and its kind. A prerequisite without a kind fails the coverage test.
PREREQ_TARGET = {"base": ("lic1-t-base", "fixture"), "fm": ("lic1-t-fm", "fixture"), "vec_int8_hnsw": ("lic1-t-int8_hnsw", "fixture"),
                 "vec_bbq_hnsw": ("lic1-t-bbq_hnsw", "fixture"), "vec_bbq_disk": ("lic1-t-bbq_disk", "fixture"),
                 "shared_base": ("lic1-base", "shared"), "sem_mapping": ("lic1-t-sem", "create"), "perc_index": ("lic1-t-perc", "create"),
                 "alias_index": ("lic1-t-gen1", "create"), "sem_doc": ("doc:lic1-t-sem", "doc"), "perc_doc": ("doc:lic1-t-perc", "doc"),
                 "inference_ep_new": ("endpoint:lic-ep-t", "endpoint"), "stub_endpoint": ("endpoint:lic-stub", "endpoint"),
                 "dls_principal": ("user", "principal")}
MODES = {"fixture": ("503", "transport", "truncated", "empty", "wrong_count", "create_fail"),
         "shared": ("503", "transport", "truncated", "empty", "wrong_count", "create_fail"),
         "create": ("create_fail", "transport", "503"),
         "doc": ("503", "transport", "empty", "wrong_count", "licence"),
         "endpoint": ("create_fail", "transport", "503"),
         "principal": ("create_fail", "transport", "503", "no_pwfile")}
# the cell whose own observation IS the prerequisite's call: not a dependent, so it may legitimately be non-AVAILABLE under that injection
PROBE_OF = {"sem_mapping": {"semantic_text_mapping", "semantic_text_index"}, "sem_doc": {"semantic_text_index"}, "inference_ep_new": {"inference_create"}}


class MatrixHarness(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._r = (matrix.req, lib.req, exp3.req)
        self._pw = matrix.DLS_PW
        self.tmp = tempfile.mkdtemp()
        matrix.DLS_PW = os.path.join(self.tmp, "dls_password")

    def tearDown(self):
        import shutil
        matrix.req, lib.req, exp3.req = self._r
        matrix.DLS_PW = self._pw
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, fake):
        matrix.req = lib.req = exp3.req = fake.req
        return fake

    def aborts(self, fn):
        try:
            out = fn()
        except FixtureError:
            return True
        self.fail("returned normally instead of aborting: %s" % json.dumps(out, default=str)[:200])

    def world(self, prereq=None, mode=None):
        """A fake with the shared base made (as life.py does) and the DLS password file present, then one prerequisite broken."""
        f = self.use(MatrixFake())
        matrix.make_base()
        with open(matrix.DLS_PW, "w") as fh:
            fh.write("test-only-not-a-secret")
        if prereq is None:
            return f
        target, kind = PREREQ_TARGET[prereq]
        if kind == "shared":   # break the already-made shared base the way each mode would have left it
            if mode in ("truncated", "empty"):
                keep = sorted(f.docs[target])[:-1] if mode == "truncated" else []
                f.docs[target] = {k: v for k, v in f.docs[target].items() if k in keep}
                f.fired.append((target, mode))
            elif mode == "create_fail":
                f.docs.pop(target)
                f.fired.append((target, mode))
            else:
                f.inject[target] = mode
        elif mode == "no_pwfile":
            os.remove(matrix.DLS_PW)
            f.fired.append((target, mode))
        else:
            bulk_mode = kind == "fixture" and mode in ("503", "transport", "truncated", "empty")
            f.inject[("bulk:" + target) if bulk_mode else target] = mode   # creation failure stays on the index itself
            if prereq == "inference_ep_new":
                f.eps.add("lic-ep-t")   # a stale endpoint answers, so the dependent call succeeds although creation failed
        return f

    def quick(self, sem="lic3-sem"):
        import inspect
        if "est" in inspect.signature(matrix.quick_probes).parameters:
            return matrix.quick_probes(sem, matrix.establish_quick())
        return matrix.quick_probes(sem)

    def witness_problems(self, phase, out, where):
        """AVAILABLE where the feature returns hits/values must carry a non-empty witness (90faede vec cells keep theirs under `search`)."""
        bad = []
        for (ph, name), (_pre, w) in REGISTRY.items():
            v = out.get(name) if ph == phase and w != "status" else None
            if isinstance(v, dict) and v.get("class") == "AVAILABLE" and not (v.get("witness") or (v.get("search") or {}).get("witness")):
                bad.append("%s: %s AVAILABLE with an empty witness" % (where, name))
        return bad


class MatrixInvariantTests(MatrixHarness):
    """Gate review 4 F3, structurally: for EVERY registered cell and EVERY prerequisite it declares, each failure mode leaves the cell not AVAILABLE;
    run-scope prerequisites abort the run. Cells come from matrix.CELLS (the code's own registry), so a new cell is covered when it is registered."""

    def test_every_prerequisite_has_an_injector(self):
        used = {p for pre, _w in REGISTRY.values() for p in pre}
        self.assertEqual(used - set(PREREQ_TARGET), set(), "prerequisite without a failure injector")
        self.assertEqual(set(PREREQ_SCOPE) - set(PREREQ_TARGET), set(), "declared prerequisite without a failure injector")

    def test_healthy_world_every_run_cell_available_with_witness(self):
        self.world()
        T = matrix.run("t")
        cells = {n for (ph, n) in REGISTRY if ph == "run"}
        self.assertEqual(cells - set(T), set(), "registered cell missing from the run output")
        bad = {n: T[n].get("class", "NO_CLASS") for n in cells if T[n].get("class") != "AVAILABLE"}
        self.assertEqual(bad, {}, "a healthy fixture must yield AVAILABLE (else the invariant below is vacuous)")
        self.assertEqual(self.witness_problems("run", T, "healthy"), [])

    def test_no_dependent_run_cell_is_available_when_a_prerequisite_fails(self):
        problems = []
        for prereq in sorted({p for (ph, _n), (pre, _w) in REGISTRY.items() if ph == "run" for p in pre}):
            for mode in MODES[PREREQ_TARGET[prereq][1]]:
                if mode == "no_pwfile":   # run() writes the password file itself (ensure_dls); only the quick probes can lack it
                    continue
                where = "%s/%s" % (prereq, mode)
                f = self.world(prereq, mode)
                try:
                    T = matrix.run("t")
                except FixtureError:
                    if PREREQ_SCOPE.get(prereq) != "run":
                        problems.append("%s: aborted the whole run (cell-scope prerequisite)" % where)
                    continue
                if not f.fired:   # the code never looked at the prerequisite; the dependent cells are still checked below
                    problems.append("%s: injection never fired (prerequisite never checked)" % where)
                if PREREQ_SCOPE.get(prereq) == "run":
                    problems.append("%s: run-scope prerequisite did not abort" % where)
                for (ph, name), (pre, _w) in REGISTRY.items():
                    if ph != "run":
                        continue
                    if name not in T:
                        problems.append("%s: cell %s missing" % (where, name))
                    elif prereq in pre and T[name].get("class") == "AVAILABLE":
                        problems.append("%s: %s AVAILABLE (witness %s)" % (where, name, T[name].get("witness")))
                    elif prereq not in pre and name not in PROBE_OF.get(prereq, ()) and T[name].get("class", "AVAILABLE") != "AVAILABLE":
                        problems.append("%s: unrelated cell %s became %s" % (where, name, T[name]["class"]))   # classless cells: see the healthy test
                problems += self.witness_problems("run", T, where)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_no_dependent_quick_cell_is_available_when_a_prerequisite_fails(self):
        problems = []
        for prereq in sorted({p for (ph, _n), (pre, _w) in REGISTRY.items() if ph == "quick" for p in pre}):
            for mode in MODES[PREREQ_TARGET[prereq][1]]:
                f = self.world(prereq, mode)
                f.docs["lic3-sem"] = {"0": {"body": "paris"}}
                P = self.quick()
                for (ph, name), (pre, _w) in REGISTRY.items():
                    if ph == "quick" and prereq in pre and P[name][0] == "AVAILABLE":
                        problems.append("%s/%s: quick %s AVAILABLE" % (prereq, mode, name))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_quick_semantic_probe_on_an_empty_index_is_not_available(self):
        f = self.world()
        f.docs["lic3-sem"] = {}   # the up-direction case: the polled index exists but no document has been written yet
        self.assertNotEqual(self.quick()["semantic_query"][0], "AVAILABLE")

    def test_healthy_quick_probes_available(self):
        f = self.world()
        f.docs["lic3-sem"] = {"0": {"body": "paris"}}
        P = self.quick()
        self.assertEqual({n: P[n][0] for (ph, n) in REGISTRY if ph == "quick" and P[n][0] != "AVAILABLE"}, {})
        self.assertEqual(len(P["dls_search"]), 3)


class MatrixVerdictTests(MatrixHarness):
    """Precedence and single-path rules of the new code (new code only: 90faede has no verdict function)."""

    def test_observed_licence_refusal_is_kept_over_unestablished_prerequisite(self):
        v = matrix.verdict("run", "semantic_query", {"sem_mapping": True, "sem_doc": {"failure": "write 403"}}, 403, MatrixFake.LICENCE)
        self.assertEqual(v["class"], "LICENCE_ERROR")
        self.assertIn("sem_doc", v["unestablished"])

    def test_refused_probe_write_is_licence_error(self):
        self.world(None)
        f = self.use(MatrixFake(inject={"bulk:lic1-t-bbq_disk": "licence"}))
        matrix.make_base()
        with open(matrix.DLS_PW, "w") as fh:
            fh.write("test-only-not-a-secret")
        T = matrix.run("t")
        self.assertEqual(T["vec_bbq_disk"]["class"], "LICENCE_ERROR")
        self.assertEqual(T["knn_rescore_vector"]["class"], "AVAILABLE")
        self.assertTrue(f.fired)

    def test_unestablished_prerequisite_nests_the_raw_observation(self):
        v = matrix.verdict("run", "knn_rescore_vector", {"vec_int8_hnsw": {"failure": "count 39"}}, 200, {"hits": {"hits": [{"_id": "7"}]}}, ["7"])
        self.assertEqual((v["class"], v["prerequisite"], v["observed"]["class"]), (matrix.UNVERIFIED, "vec_int8_hnsw", "AVAILABLE"))

    def test_2xx_without_hits_is_empty_witness(self):
        self.assertEqual(matrix.verdict("run", "bm25_match", {"base": True}, 200, {}, [])["class"], matrix.EMPTY)

    def test_unregistered_or_duplicate_cell_is_refused(self):
        K = matrix.Cells("run", {})
        with self.assertRaises(KeyError):
            K.put("no_such_cell", 200)
        K.put("inference_list", 200, {}, [])
        with self.assertRaises(KeyError):
            K.put("inference_list", 200, {}, [])
        with self.assertRaises(RuntimeError):
            K.done()

    def test_cells_are_decided_only_by_verdict(self):
        import ast
        tree = ast.parse(open(os.path.join(LIC, "matrix.py")).read())
        callers = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "verdict":
                        callers.add(fn.name)
        self.assertEqual(callers - {"Cells", "put"}, set(), "verdict() called outside Cells.put")
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        self.assertNotIn("cell", imported, "lib.cell would decide a class outside verdict()")
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("run", "quick_probes")):
            for n in ast.walk(fn):
                if isinstance(n, ast.Dict):
                    self.assertNotIn("class", [getattr(k, "value", None) for k in n.keys], "class literal in %s" % fn.name)

    def test_not_observed_classes_agree(self):
        self.assertEqual(tuple(recompute.NOT_OBSERVED), tuple(matrix.NOT_OBSERVED))

    def test_moment_analysis_does_not_count_unverified_dls_as_refusal(self):
        rows = [{"_t1": t, "dls_search": [matrix.UNVERIFIED, None, None]} for t in (1.0, 2.0, 3.0)]
        self.assertEqual(exp2.moment_analysis(rows, 2.0, features=("dls_search",))["dls_search"], {"skipped": True})

    def test_callsites_table_matches_registry(self):
        doc = open(os.path.join(LIC, "CALLSITES.md")).read()
        for (ph, name), (pre, w) in REGISTRY.items():
            self.assertIn("| %s | %s | %s | %s |" % (ph, name, ", ".join(pre) or "none", w), doc, name)
        for p, (scope, desc) in matrix.PREREQS.items():
            self.assertIn("| %s | %s |" % (p, scope), doc, p)


class MatrixFixtureTests(MatrixHarness):
    def test_good_fixture_is_accepted(self):
        f = self.use(MatrixFake())
        matrix.make_base()
        self.assertEqual(len(f.docs[matrix.BASE_IDX]), len(matrix.DOCS))

    def test_failed_creation_aborts(self):
        self.use(MatrixFake(inject={matrix.BASE_IDX: "create_fail"}))
        self.aborts(matrix.make_base)

    def test_failed_fixture_write_aborts(self):
        self.use(MatrixFake(inject={"bulk:" + matrix.BASE_IDX: "503"}))
        self.aborts(matrix.make_base)

    def test_partial_fixture_write_aborts(self):
        self.use(MatrixFake(inject={"bulk:" + matrix.BASE_IDX: "truncated"}))
        self.aborts(matrix.make_base)

    def test_empty_index_after_success_answers_aborts(self):
        self.use(MatrixFake(inject={"bulk:" + matrix.BASE_IDX: "empty"}))
        self.aborts(matrix.make_base)

    def test_full_matrix_aborts_on_failed_base_write(self):
        self.world("base", "503")
        self.aborts(lambda: matrix.run("t"))

    def test_full_matrix_aborts_on_empty_force_merge_fixture(self):
        self.world("fm", "empty")
        self.aborts(lambda: matrix.run("t"))

    def test_rescore_cell_not_available_after_bulk_503_into_existing_index(self):   # review 4 (a), its first case
        f = self.world("vec_int8_hnsw", "503")
        T = matrix.run("t")
        self.assertIn(("bulk:lic1-t-int8_hnsw", "503"), f.fired)
        self.assertNotEqual(T["knn_rescore_vector"]["class"], "AVAILABLE")

    def test_dls_battery_aborts_on_empty_shared_base(self):
        self.world("shared_base", "empty")
        self.aborts(matrix.dls_battery)

    def test_summarize_matrix_table_accepts_new_cells(self):
        self.world()
        T = matrix.run("t")
        tab = summarize.matrix_table({"exp1_basic0": T, "exp1_trial": T, "exp1_after": T})
        self.assertTrue(tab)

    def test_vector_and_rescore_cells_are_not_available_when_index_is_empty(self):   # review 4 (a): the rescore cell too
        self.world("vec_int8_hnsw", "empty")
        T = matrix.run("t")
        self.assertNotEqual(T["vec_int8_hnsw"]["class"], "AVAILABLE")
        self.assertNotEqual(T["knn_rescore_vector"]["class"], "AVAILABLE")
        self.assertEqual(T["vec_bbq_hnsw"]["class"], "AVAILABLE")

    def test_semantic_searches_not_available_after_failed_document_write(self):   # review 4 (b)
        self.world("sem_doc", "503")
        T = matrix.run("t")
        self.assertNotEqual(T["semantic_query"]["class"], "AVAILABLE")
        self.assertNotEqual(T["match_on_semantic_text"]["class"], "AVAILABLE")

    def test_exp3_semantic_only_fixture_with_failed_write_aborts(self):
        self.use(MatrixFake(inject={"bulk:" + exp3.SEMONLY: "503"}))
        with self.assertRaises((ScrollError, BulkError)):
            exp3.semonly_fixture()


class Exp2VerifyWritesTests(unittest.TestCase):
    """exp2.verify_writes read-backs must succeed (same defect class: a failed read made leak counts vacuously zero)."""

    def _w(self):
        w = object.__new__(exp2.Workload)
        w.acked, w.attempted_failed, w.sem_rows = {}, [], [[0.0, "s1", 201, None]]
        return w

    def _run(self, sem_status):
        def fake(method, path, body=None, **kw):
            if "lic-sem2/_search" in path:
                return (sem_status, {"hits": {"total": {"value": 1}, "hits": [{"_id": "s1"}]}, "_shards": {"failed": 0}} if sem_status == 200 else {"error": {}}, 1)
            if "_search" in path and "scroll" in path:
                return 200, {"_scroll_id": "x", "hits": {"hits": []}, "_shards": {"failed": 0}}, 1
            return 200, {"_shards": {"failed": 0}}, 1
        old = exp2.req
        exp2.req = fake
        try:
            return self._w().verify_writes()
        finally:
            exp2.req = old

    def test_failed_semantic_read_back_raises(self):
        with self.assertRaises(AssertionError):
            self._run(503)

    def test_failed_live_scroll_page_raises(self):
        def fake(method, path, body=None, **kw):
            if "_search?scroll" in path:
                return 503, {"error": {}}, 1
            return 200, {"_shards": {"failed": 0}}, 1
        old = exp2.req
        exp2.req = fake
        try:
            with self.assertRaises(AssertionError):
                self._w().verify_writes()
        finally:
            exp2.req = old

    def test_empty_backend_oracle_counts_as_failure(self):
        w = object.__new__(exp2.Workload)
        w.queries, w.oracle_rrf, w.oracle_lin = [("q", [0.0])], {}, {}
        old = exp2.oracle.raw_backend
        exp2.oracle.raw_backend = lambda *a, **k: (200, [], None)
        try:
            self.assertEqual(w.compute_backend_oracles(), 2)
            self.assertEqual(w.oracle_rrf, {})
        finally:
            exp2.oracle.raw_backend = old

    def test_t4_does_not_pass_on_unverified_dls_principal(self):
        R = load("down_1.json")
        R["exp1_after"]["dls_search_as_user"] = {"class": "UNVERIFIED_PREREQUISITE", "status": 401, "returned": None}
        self.assertFalse(summarize.exp1_checks(R)["T4_pass"])

    def test_good_semantic_read_back_counts(self):
        self.assertEqual(self._run(200)["sem_acked_absent"], 0)


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
