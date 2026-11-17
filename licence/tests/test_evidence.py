"""Typed-evidence verdict tests (DESIGN-VERDICTS.md). No node needed. Run: cd licence && python3 -m unittest tests.test_evidence

Scenario map (DESIGN-VERDICTS.md section 1). Each scenario is a regression here, or in test_gates.py where it is already exercised through
the harness fakes:
- F2 bulk without items, unchecked migration status: test_gates bulk-wire tests; here test_a_outcome_* (1xx/3xx/401 never AVAILABLE).
- F3 dependent cells after a failed fixture: test_gates MatrixInvariantTests and MatrixFixtureTests.
- F6 skipped mandatory probe: test_f6_*.  F8 migrated operations: test_f8_*.  F9 DLS control and refusal class: test_f9_*.
- F10 empty campaign: test_f10_*.  F11 sensitivity control counts: test_f11_*.  F12 domains: test_f12_*.
GeneratedInvalidEvidenceTests builds invalid evidence from the evidence type definitions (every leaf field's domain, every declared
cross-field relation, every outcome case other than the intended one) and asserts no PASS and no exception.
"""
import ast
import copy
import inspect
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIC = os.path.dirname(HERE)
RES = os.path.join(LIC, "results")
sys.path.insert(0, LIC)
sys.path.insert(0, os.path.dirname(LIC))
import lib  # noqa: E402
import summarize  # noqa: E402

INF = float("inf")
# Intended HTTP statuses per outcome case, declared HERE independently of evidence.Outcome (the generator must not ask the classifier
# under test which statuses are acceptable): only a 403 is ever a refusal.
INTENDED_STATUS = {"SUCCESS": set(range(200, 300)), "NOT_FOUND": {404}, "AUTHORISATION_REFUSAL": {403}, "LICENCE_REFUSAL": {403}}


_LIVES = {}


def _life(name):
    """A committed life as summarize.load sees it (loaded once per mode, then copied)."""
    mode = name.split("_")[0]
    if mode not in _LIVES:
        _LIVES[mode] = {R["_file"][:-5]: R for R in summarize.load(RES, mode)}
    return copy.deepcopy(_LIVES[mode][name])


def _healthy(scope):
    """Known-good fixture: committed down_7 / up_1 as the summarizer loads them, with addendum C's sem_failed_other (never recorded) set
    so that EXP2-T4 can pass. Every other value is as committed."""
    R = _life("down_7" if scope in ("down", "both") else "up_1")
    R["exp2"]["writes"]["sem_failed_other"] = 0
    if scope in ("down", "both"):
        _with_adapter_view(R)
    return R


LATCH_CELLS = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
HEALTHY_LATCH_BODY = (b'{"error":{"root_cause":[{"type":"security_exception","reason":"current license is non-compliant for [rrf]"}],'
                      b'"type":"security_exception","reason":"current license is non-compliant for [rrf]"},"status":403}')


def _with_adapter_view(R):
    """Review 9 F18: the committed latch cells carry no adapter message, so EXP1-T3 is UNVERIFIED on them. The healthy fixture adds
    what matrix.verdict now records: the adapter's message for a licence 403 and the adapter detector's answer on it."""
    import observe
    msg = observe.adapter_message(403, HEALTHY_LATCH_BODY)
    for c in LATCH_CELLS:
        R["exp1_after"][c].update(adapter_message=msg, adapter_detects=observe.adapter_detects(msg))
    return R


def _set(R, path, val):
    d = R
    for k in path[:-1]:
        d = d[k]
    if val == "absent":
        del d[path[-1]]
    else:
        d[path[-1]] = val


def _status(R, report, key, mode="down"):
    if report == "exp1":
        return summarize.exp1_checks(R).get(key)
    if report == "exp2":
        return summarize.exp2_checks(R, mode).get(key)
    return summarize.exp3_checks(R)["migration_checks"].get(key) if key == "migration_status" else summarize.exp3_checks(R).get(key)


class ScenarioRegressionTests(unittest.TestCase):
    """One regression per scenario of gate reviews 2 to 7, through the public summarizer and harness API (reachable on 4761862)."""

    def assertNotPass(self, life, report, key, path, val, mode="down", exact=None):
        R = _life(life) if not life.startswith("healthy") else _healthy(life.split("_")[1])
        _set(R, path, val)
        st = _status(R, report, key, mode)
        self.assertNotEqual(st, "PASS", (path, val))
        if exact:
            self.assertEqual(st, exact, (path, val))

    def test_a_outcome_1xx_3xx_401_are_never_available(self):
        for code in (101, 199, 301, 302, 304, 401):
            self.assertNotEqual(lib.classify(code, {}), "AVAILABLE", code)
        self.assertEqual(lib.classify(200, {}), "AVAILABLE")
        self.assertEqual(lib.classify(0, None), "TRANSPORT")
        self.assertEqual(lib.classify(403, {"error": {"reason": "current license is non-compliant for [x]"}}), "LICENCE_ERROR")

    def test_a_licence_text_on_a_non_403_is_not_a_refusal(self):
        """Redesign review: a 5xx, 401, 404 or 409 carrying the licence text is not the 403 licence refusal the latch and DLS gates need."""
        for code in (500, 503, 401, 404, 409):
            self.assertNotPass("down_7", "exp1", "T3_latch_status", ("exp1_after", "retriever_rrf", "status"), code)
            R = _life("down_7")
            R["exp1_after"]["dls_search_as_user"].update(status=code, error={"reason": "current license is non-compliant for [security]"})
            self.assertEqual(summarize.exp1_checks(R)["T4_status"], "UNVERIFIED", code)
        R = _life("down_7")
        R["exp1_after"]["retriever_rrf"]["error"] = {"reason": "action unauthorized for user"}
        self.assertNotEqual(summarize.exp1_checks(R)["T3_latch_status"], "PASS")

    def test_f6_skipped_mandatory_probe_is_unverified(self):
        for p in ("dls_search", "rrf"):
            self.assertNotPass("down_7", "exp1", "T2_status", ("exp1_moment", p), {"skipped": True, "unverified": True, "n": 10, "classes": []},
                               exact="UNVERIFIED")
            self.assertNotPass("down_7", "exp1", "T2_status", ("exp1_moment", p), "absent", exact="UNVERIFIED")

    def test_f8_migrated_operations_missing_or_failed(self):
        ops = ("exp3", "migration", "migrated_basic_ops")
        self.assertNotPass("down_7", "exp3", "migration_status", ops, {}, exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp3", "migration_status", ops + ("lic3-sem-mig", "force_merge"), "absent", exact="UNVERIFIED")
        for code in (0, 302, 401, 404, 409, 503):
            self.assertNotPass("down_7", "exp3", "migration_status", ops + ("lic3-bbq_disk-mig", "update"), code, exact="FAIL")
        for bad in (-1, 1000, 200.0, "200", True, None):
            self.assertNotPass("down_7", "exp3", "migration_status", ops + ("lic3-bbq_disk-mig", "update"), bad, exact="UNVERIFIED")

    def test_f9_dls_control_and_refusal_class(self):
        for val in ("absent", 401, 503, 0):
            self.assertNotPass("down_7", "exp1", "T4_status", ("exp1_trial", "dls_search_as_user", "status"), val, exact="UNVERIFIED")
        for val in (401, 503, 0, 404, 302):
            self.assertNotPass("down_7", "exp1", "T4_status", ("exp1_after", "dls_search_as_user", "status"), val, exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp1", "T4_status", ("exp1_after", "dls_search_as_user", "status"), 200, exact="FAIL")
        for val in (0, 8, 9, -1, 2.5):
            self.assertNotPass("down_7", "exp1", "T4_status", ("exp1_trial", "dls_search_as_user", "returned"), val, exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp1", "T4_battery_status", ("dls_battery_trial",), {}, exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp1", "T4_battery_status", ("dls_battery_trial", "esql"), "absent", exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp1", "T4_battery_status", ("dls_battery_after", "esql", "status"), 401, exact="UNVERIFIED")
        self.assertNotPass("down_7", "exp1", "T4_battery_status", ("dls_battery_trial", "get_hidden_doc1", "returned_hidden_doc"), True,
                           exact="UNVERIFIED")

    def test_f10_empty_campaign_is_unverified(self):
        agg = summarize.aggregate(tempfile.mkdtemp(), {})
        self.assertEqual(set(agg["all_status"].values()), {"UNVERIFIED"})

    def test_f11_control_counts_are_required_and_the_rate_is_computed(self):
        c = ("exp2", "T1_control_broken_fallback")
        self.assertEqual(_status(_life("down_7"), "exp2", "T1_status"), "PASS")
        for key in ("client_labelled", "flagged_wrong", "backend_labelled_checked", "backend_wrong"):
            for val in ("absent", "x", None, -1, 2.5, INF, True):
                self.assertNotPass("down_7", "exp2", "T1_status", c + (key,), val, exact="UNVERIFIED")
        for key in ("client_labelled", "backend_labelled_checked"):
            self.assertNotPass("down_7", "exp2", "T1_status", c + (key,), 0, exact="UNVERIFIED")
        R = _life("down_7")
        R["exp2"]["T1_control_broken_fallback"]["flagged_wrong"] = int(R["exp2"]["T1_control_broken_fallback"]["client_labelled"] * 0.8)
        R["exp2"]["T1_control_broken_fallback"]["flag_rate"] =R["exp2"]["T1_control_broken_fallback"]["flagged_wrong"] / R["exp2"]["T1_control_broken_fallback"]["client_labelled"]
        self.assertEqual(_status(R, "exp2", "T1_status"), "FAIL")
        R = _life("down_7")   # counts say 80%, the stored derived rate still says 1.0: the record is inconsistent
        R["exp2"]["T1_control_broken_fallback"]["flagged_wrong"] = int(R["exp2"]["T1_control_broken_fallback"]["client_labelled"] * 0.8)
        self.assertEqual(_status(R, "exp2", "T1_status"), "UNVERIFIED")
        self.assertNotPass("down_7", "exp2", "T1_status", c + ("flag_rate",), 0.95, exact="UNVERIFIED")
        R = _life("down_7")
        R["exp2"]["T1_control_broken_fallback"]["flagged_wrong"] = R["exp2"]["T1_control_broken_fallback"]["client_labelled"] + 1
        self.assertEqual(_status(R, "exp2", "T1_status"), "UNVERIFIED")

    def test_f12_values_outside_their_domain(self):
        cases = [("down_7", "exp2", "T2_status", ("exp2", "T2_failed_requests"), v) for v in (-1, 0.5, INF, 10 ** 9)]
        cases += [("down_7", "exp2", "T1_status", ("exp2", "T1_control_broken_fallback", "flag_rate"), v) for v in (INF, 2.0, -0.1)]
        cases += [("down_7", "exp2", "T5_status", ("exp2", "T5_control_after_1s", "licence_failures"), v) for v in (INF, 1e9, 10 ** 9, -1)]
        cases += [("up_1", "exp2", "T5_status", ("exp2", "T5_control_before_start_trial", "licence_failures"), v) for v in (INF, 1e9, 10 ** 9)]
        cases += [("down_7", "exp2", "T2_status", ("exp2", "T2_longest_failure_window_s"), v) for v in (-1.0, -INF, INF)]
        cases += [("down_7", "exp2", "T3_status", ("exp2", "T3_flip_latency_s"), v) for v in (-INF, -5.0, INF)]
        cases += [("up_1", "exp2", "T3_status", ("exp2", "T3_flip_latency_s"), v) for v in (-0.1, -INF)]
        cases += [("down_7", "exp1", "T2_status", ("exp1_moment", "rrf", "first_refusal_rel_resp_s"), v) for v in (-INF, -3.0)]
        cases += [("down_7", "exp3", "migration_status", ("exp3", "migration", "bbq_disk_to_hnsw", "src_vs_ground_truth_max_abs"), v) for v in (-1, INF)]
        cases += [("down_7", "exp3", "migration_status", ("exp3", "migration", "semantic_to_text_vector", "worst_cosine_vs_stub"), v) for v in (1.5, INF)]
        cases += [("down_7", "exp2", "T2_status", ("exp2", "T2_failure_windows"), [[10.0, 10.5, 3]])]   # windows contradict 0 failures
        for life, report, key, path, val in cases:
            self.assertNotPass(life, report, key, path, val, mode=life.split("_")[0], exact="UNVERIFIED")


T_RESP = 1000.0
PROBES = ("rrf", "linear", "semantic_query", "inference_use", "dls_search")


def _poll(after, before=("AVAILABLE", 200)):
    """Synthetic poll rows as matrix.quick_probes records them ([class, status] per probe; dls also the returned count): 20 polls before
    the transition response, 40 after. `after` maps probe -> (class, status) observed after it (default: the healthy refusal)."""
    healthy = {p: ("OTHER_ERROR", 403) if p == "dls_search" else ("LICENCE_ERROR", 403) for p in PROBES}
    healthy.update(after)
    rows = []
    for k in range(60):
        t = T_RESP - 1.0 + k * 0.05
        row = {"_t0": t - 0.01, "_t1": t}
        for p in PROBES:
            cls, st = before if t < T_RESP else healthy[p]
            row[p] = [cls, st] + ([4 if cls == "AVAILABLE" else None] if p == "dls_search" else [])
        rows.append(row)
    return rows


class RecordingBoundaryTests(unittest.TestCase):
    """Gate review 8 (F13 to F16), end to end from the recorded observation through the verdict: the harness's own moment_analysis turns
    poll rows into the life record; the summarizer judges that record."""

    def t2(self, after):
        import exp2
        R = _life("down_7")
        R["exp1_moment"] = exp2.moment_analysis(_poll(after), T_RESP)
        return summarize.exp1_checks(R)["T2_status"]

    def test_f13_healthy_recorded_refusals_pass(self):
        self.assertEqual(self.t2({}), "PASS")

    def test_f13_dls_401_503_transport_are_not_a_refusal(self):
        for obs in (("OTHER_ERROR", 401), ("OTHER_ERROR", 503), ("TRANSPORT", 0), ("OTHER_ERROR", 404), ("OTHER_ERROR", 429)):
            self.assertNotEqual(self.t2({"dls_search": obs}), "PASS", obs)

    def test_f13_dls_licence_refusal_is_not_the_dls_refusal(self):
        self.assertNotEqual(self.t2({"dls_search": ("LICENCE_ERROR", 403)}), "PASS")

    def test_f13_licence_text_on_503_is_not_a_licence_refusal(self):
        self.assertNotEqual(self.t2({p: ("LICENCE_ERROR", 503) for p in PROBES[:4]}), "PASS")
        self.assertNotEqual(self.t2({"rrf": ("OTHER_ERROR", 403)}), "PASS")   # a 403 without the licence marker on a licence probe

    def test_f13_recorded_refusal_status_outside_403_is_not_a_pass(self):
        for s in (101, 302, 429, 503, 0):
            R = _life("down_7")
            for p in PROBES:
                m = R["exp1_moment"][p]
                m["first_refusal_status"] = s
                if isinstance(m.get("first_refusal"), dict):
                    m["first_refusal"]["status"] = s
            self.assertNotEqual(summarize.exp1_checks(R)["T2_status"], "PASS", s)

    def test_f13_a_timing_without_its_refusal_outcome_is_unverified(self):
        """Committed down_1..6 store only a timing and a bare status per probe (no class, no body marker, no raw poll rows): no PASS."""
        for n in range(1, 7):
            self.assertEqual(summarize.exp1_checks(_life("down_%d" % n))["T2_status"], "UNVERIFIED", n)

    def test_f13_raw_rows_are_required_for_rederived_figures(self):
        """A life file copied without its raw rows: the moment (poll.gz) and the T5 licence-refusal count (rows.gz) cannot be re-derived."""
        import shutil
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(RES, "down_7.json"), d)
            R = summarize.load(d, "down")[0]
            self.assertEqual(summarize.exp1_checks(R)["T2_status"], "UNVERIFIED")
            self.assertEqual(summarize.exp2_checks(R, "down")["T5_status"], "UNVERIFIED")
        finally:
            shutil.rmtree(d)

    def test_f13_cell_class_labels_licence_text_only_on_403(self):
        body = {"error": {"type": "x", "reason": "current license is non-compliant for [security]"}}
        for code in (500, 503, 401, 404, 409):
            self.assertNotEqual(lib.classify(code, body), "LICENCE_ERROR", code)
        self.assertEqual(lib.classify(403, body), "LICENCE_ERROR")

    def test_f13_semantic_write_failure_class_uses_the_outcome(self):
        """The EXP2-T4 'loud licence failure' class of a failed semantic write: same Outcome rule (derived-field audit)."""
        import exp2
        body = {"type": "security_exception", "reason": "current license is non-compliant for [inference]"}
        self.assertEqual(exp2.classify_sem_failure(403, body), "licence")
        for code in (503, 500, 429, 0):
            self.assertEqual(exp2.classify_sem_failure(code, body), "other", code)

    def test_f14_huge_and_odd_values_never_raise(self):
        cases = [(("exp2", "T2_failed_requests"), 10 ** 400), (("exp2", "T2_longest_failure_window_s"), 10 ** 400),
                 (("exp2", "T1_control_broken_fallback", "flag_rate"), -10 ** 400), (("exp2", "T3_flip_latency_s"), 10 ** 400),
                 (("exp2", "T2_failure_windows"), [[0, 10 ** 400, 1]]), (("exp2", "T2_failed_requests"), "5"), (("exp2", "T2_failed_requests"), -0.0)]
        for path, val in cases:
            R = _life("down_7")
            _set(R, path, val)
            r = summarize.exp2_checks(R, "down")
            gate = "T1_status" if path[1].startswith("T1") else "T2_status" if path[1].startswith("T2") else "T3_status"
            self.assertNotEqual(r[gate], "PASS", (path, val))   # the gate that reads the mutated field

    # ---- review 8 follow-up (advisor pass before return): the same classes inside the new boundary code
    def test_f13_refusal_licence_flag_is_a_declared_domain(self):
        """A first_refusal without its licence flag, or with a non-bool flag, is not a refusal of either kind."""
        for bad in ("absent", None, 1, "yes", {"reason": "x"}):
            R = _life("down_7")
            fr = R["exp1_moment"]["dls_search"]["first_refusal"]
            _set(R, ("exp1_moment", "dls_search", "first_refusal", "licence_text"), bad)
            self.assertEqual(summarize.exp1_checks(R)["T2_status"], "UNVERIFIED", bad)
            self.assertTrue(fr is not None)

    def test_f13_successes_and_refusals_are_counted_by_outcome_not_label(self):
        import observe
        for bad in (["AVAILABLE", 403], ["OTHER_ERROR", 200], ["LICENCE_ERROR", 200]):   # label contradicts status (review 9 F17)
            self.assertRaises(observe.Unparsed, observe.parse_cell, bad, ())
            rows = _poll({})
            rows[-1]["rrf"] = bad
            m = observe.moments(rows, T_RESP)
            self.assertIn("stream_error", m["rrf"], bad)          # the whole stream is unparsed: no refusal, T2 UNVERIFIED
            self.assertIsNone(m["rrf"].get("first_refusal"), bad)
        for cell in (["EMPTY_WITNESS", 200], ["AVAILABLE", 200]):   # any 2xx answer after the refusal
            rows = _poll({})
            rows[-1]["rrf"] = cell
            m = observe.moments(rows, T_RESP)
            self.assertEqual(m["rrf"]["success_after_first_refusal"], 1, cell)
        rows = _poll({"dls_search": ("AVAILABLE", 403)})
        self.assertIsNone(observe.moments(rows, T_RESP)["dls_search"].get("first_refusal"))

    def test_f14_load_never_raises_on_bad_transition_times_or_raw_rows(self):
        import gzip
        import shutil
        for life, key, field in (("down_7", "start_basic", "t_resp"), ("up_1", "start_trial", "t_req")):
            for bad in ("absent", 10 ** 400, "5", None, [1], float("nan")):
                d = tempfile.mkdtemp()
                try:
                    for ext in (".rows.gz", ".poll.gz"):
                        if os.path.exists(os.path.join(RES, life + ext)):
                            shutil.copy(os.path.join(RES, life + ext), d)
                    R = json.load(open(os.path.join(RES, life + ".json")))
                    _set(R, (key, field), bad)
                    json.dump(R, open(os.path.join(d, life + ".json"), "w"))
                    L = summarize.load(d, life.split("_")[0])   # must not raise
                    self.assertEqual(len(L), 1)
                finally:
                    shutil.rmtree(d)
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(RES, "down_7.json"), d)
            with gzip.open(os.path.join(d, "down_7.rows.gz"), "wt") as fh:
                fh.write('[1, 2, "ctl"]\n{"a": 1}\n')
            with gzip.open(os.path.join(d, "down_7.poll.gz"), "wt") as fh:
                fh.write('[{"rrf": [1]}, 5]')
            R = summarize.load(d, "down")[0]
            self.assertEqual(summarize.exp1_checks(R)["T2_status"], "UNVERIFIED")
        finally:
            shutil.rmtree(d)

    def test_f14_reports_never_raise_on_odd_values(self):
        """Every leaf a gate reads, set to each odd value, through every report function (not only judge)."""
        import verdicts
        odd = (10 ** 400, "5", {"a": {"b": []}}, "x" * 100000, True, -0.0, [[[]]])
        gen = GeneratedInvalidEvidenceTests()
        gen.setUpClass()
        problems = []
        base = {"down": _healthy("down"), "up": _healthy("up")}
        for name, g in verdicts.GATES.items():
            mode = "up" if g.scope == "up" else "down"
            R = base[mode]
            for fname, path, f in gen.leaves(g.evidence, ()):
                parent = summarize.fetch(R, path[:-1])
                if not isinstance(parent, dict) or path[-1] not in parent:
                    continue
                keep = parent[path[-1]]
                for v in odd:
                    parent[path[-1]] = v
                    try:
                        summarize.exp2_checks(R, mode)
                        if mode == "down":
                            summarize.exp1_checks(R)
                            summarize.exp3_checks(R)
                    except Exception as e:  # noqa: BLE001
                        problems.append("%s %s=%r: %s" % (name, ".".join(path), str(v)[:20], type(e).__name__))
                parent[path[-1]] = keep
        self.assertEqual(problems, [], "\n".join(problems[:40]))

    def test_f14_aggregate_never_raises_on_odd_values(self):
        import verdicts
        gen = GeneratedInvalidEvidenceTests()
        gen.setUpClass()
        orig = summarize.load
        for v in (10 ** 400, "5", {"a": {"b": []}}, True):
            lives = {"down": _healthy("down"), "up": _healthy("up")}
            for name, g in verdicts.GATES.items():
                R = lives["up" if g.scope == "up" else "down"]
                for fname, path, f in gen.leaves(g.evidence, ()):
                    parent = summarize.fetch(R, path[:-1])
                    if isinstance(parent, dict) and path[-1] in parent:
                        parent[path[-1]] = v
            summarize.load = lambda d, mode, L=lives: [copy.deepcopy(L[mode])]
            try:
                summarize.aggregate(RES, {})   # must not raise
            finally:
                summarize.load = orig

    def test_f15_status_values_are_exact_outcomes(self):
        import evidence as E
        Sub = type("Sub", (E.Outcome,), {})
        with self.assertRaises(E.Unparsed):
            E.LatchCell(outcome=Sub(403, "LICENCE_REFUSAL", True), message="HTTP 403: current license is non-compliant", detects=True)

    def test_f15_nested_evidence_is_checked_by_exact_type_and_containers_are_immutable(self):
        import evidence as E
        Sub = type("Sub", (E.DlsControl,), {})
        ok = E.Outcome.of(200)
        sub = Sub(outcome=ok, only_kw_a=True, returned=4)
        after = E.DlsAfter(outcome=E.Outcome.of(403), cell_class="OTHER_ERROR")
        with self.assertRaises(E.Unparsed):
            E.Dls(control=sub, after=after)
        a = E.Availability.parse({"exp2": {"T2_failed_requests": 0, "T2_non_control_requests": 5, "T2_longest_failure_window_s": 0.0,
                                           "T2_failure_windows": []}})
        self.assertIsInstance(a.windows, tuple)


class GeneratedInvalidEvidenceTests(unittest.TestCase):
    """Invalid evidence generated from the evidence type definitions; no PASS, no exception. The healthy fixture passes."""

    @classmethod
    def setUpClass(cls):
        import evidence  # noqa: F401  (absent before the redesign: these tests error, the scenario tests above fail by behaviour)
        import verdicts
        cls.E, cls.V = evidence, verdicts

    def leaves(self, ecls, prefix):
        """(absolute path, Field, owning class) for every leaf of an evidence type, recursively."""
        for name, f in ecls.SPEC.items():
            path = prefix + f.path
            if isinstance(f.kind, type) and issubclass(f.kind, self.E.Evidence):
                yield from self.leaves(f.kind, path)
            else:
                yield name, path, f

    def mutations(self, f, healthy, body):
        k = self.E.KINDS[f.kind]   # a field whose kind has no domain fails here
        out = [("None", None), ("wrong type", 123 if k.type == "text" else "x"), ("bool", True if k.type != "bool" else 1),
               ("huge int", 10 ** 400), ("-huge int", -10 ** 400), ("numeric string", "5"), ("nested list", [[[]]]), ("nested dict", {"a": {"b": []}})]
        if k.type != "text":
            out.append(("long string", "x" * 100000))
        if k.type in ("int", "status"):
            out.append(("negative zero", -0.0))
        if not f.optional:
            out.append(("absent", "absent"))
        if k.type in ("int", "real", "status"):
            out += [("inf", INF), ("-inf", -INF), ("nan", float("nan"))]
            if k.lo is not None or f.lo is not None:
                lo = f.lo if f.lo is not None else k.lo
                out.append(("below range", lo - 1 if k.type != "real" else lo - 0.001))
            if k.hi is not None or f.hi is not None:
                hi = f.hi if f.hi is not None else k.hi
                out.append(("above range", hi + 1 if k.type != "real" else hi + 0.001))
            if k.type in ("int", "status"):
                base = healthy if type(healthy) is int else 1
                out += [("non-integer", base + 0.5), ("integral float", float(base))]
        if k.type == "status":
            ok = set().union(*(INTENDED_STATUS[c] for c in f.expect))
            for s in (0, 101, 200, 204, 302, 401, 403, 404, 409, 500, 503):
                if s not in ok:
                    out.append(("status %d" % s, s))
        elif k.type == "int" and f.expect:
            out.append(("outside expect", max(f.expect) + 1))
        elif k.type == "bool" and f.expect is not None:
            out.append(("not expected", not f.expect))
        elif k.type == "text":
            out += [("empty str", ""), ("empty dict", {})]
        elif k.type == "windows":
            out += [("end before start", [[1.0, 0.5, 1]]), ("zero-count window", [[0.0, 0.5, 0]]), ("infinite end", [[0.0, INF, 1]]),
                    ("not a list", {"a": 1}), ("short entry", [[0.0, 0.5]])]
        out += [("bad", b) for b in (f.bad or ())]
        return [(lab, v) for lab, v in out if not (type(v) is type(healthy) and v == healthy and v == v)]

    def relation_mutations(self, ecls, prefix, R):
        """Every declared relation, broken with values inside each field's own domain."""
        for name, f in ecls.SPEC.items():
            if isinstance(f.kind, type) and issubclass(f.kind, self.E.Evidence):
                yield from self.relation_mutations(f.kind, prefix + f.path, R)
        absp = {n: prefix + f.path for n, f in ecls.SPEC.items()}
        absp.update({n + "." + k: prefix + f.path + g.path for n, f in ecls.SPEC.items() if isinstance(f.kind, type)
                     for k, g in f.kind.SPEC.items()})
        get = lambda n: summarize.fetch(R, absp[n])
        for rel in ecls.RELATIONS:
            kind, a = rel[0], rel[1]
            if kind == "le":
                rhs = sum(get(x) for x in (rel[2] if isinstance(rel[2], tuple) else (rel[2],)))
                if isinstance(a, tuple):
                    raise AssertionError("le relation with a summed left side has no generated mutation: %r" % (rel,))
                yield "%s.%s > %s" % (ecls.__name__, a, rel[2]), {absp[a]: rhs + 1}
            elif kind == "eq":
                yield "%s.%s != %s" % (ecls.__name__, a, rel[2]), {absp[a]: get(rel[2]) + 1.0}
            elif kind == "sum_eq":
                yield "%s sum != %s" % (ecls.__name__, rel[2]), {absp[rel[2]]: get(rel[2]) + 1}
            elif kind == "ratio_matches":
                r = get(a)
                yield "%s.%s contradicts counts" % (ecls.__name__, a), {absp[a]: r - 0.5 if r >= 0.5 else r + 0.5}
            elif kind == "ge_diff":
                yield "%s.%s before t_req" % (ecls.__name__, a), {absp[a]: get(rel[2]) - get(rel[3]) - 1.0}
            elif kind == "windows_match":
                yield "%s longest contradicts windows" % ecls.__name__, {absp[rel[2]]: get(rel[2]) + 1.0}
                yield "%s windows contradict failures" % ecls.__name__, {absp[a]: [[0.0, 0.0, get(rel[3]) + 1]]}
            else:
                raise AssertionError("relation kind without a generated mutation: %r" % (rel,))

    def test_direct_construction_enforces_the_same_invariants(self):
        E = self.E
        ok = E.Outcome.of(200)
        with self.assertRaises(E.Unparsed):
            E.Proportion(5, 3)
        with self.assertRaises(E.Unparsed):
            E.Outcome(503, "LICENCE_REFUSAL")   # a case that does not belong to the status
        with self.assertRaises(E.Unparsed):
            E.LatchCell(outcome=403, message="HTTP 403: current license is non-compliant", detects=True)   # a bare status is not an outcome
        with self.assertRaises(E.Unparsed):   # review 9 F18: a recorded detector flag that the adapter's own detector contradicts
            E.LatchCell(outcome=E.Outcome.of(403, True), message="HTTP 403: license expired", detects=True)
        with self.assertRaises(E.Unparsed):
            E.DlsControl(outcome=E.Outcome.of(401), only_kw_a=True, returned=4)   # control not established
        with self.assertRaises(E.Unparsed):
            E.DlsControl(outcome=ok, only_kw_a=True, returned=0)
        with self.assertRaises(E.Unparsed):
            E.FallbackControl(client_labelled=0, flagged_wrong=0, backend_checked=1, backend_wrong=0)
        with self.assertRaises(E.Unparsed):
            E.Writes(acked=3, acked_absent=5, sem_acked=1, sem_acked_absent=0, sem_failed=0, sem_failed_present=0, sem_failed_loud=0,
                     sem_failed_other=None, loss_control=True)   # relation: absent acknowledged writes cannot exceed acknowledged
        with self.assertRaises(E.Unparsed):
            E.Availability(failed=-1, requests=10, longest_window_s=0.0, windows=[])
        E.DlsControl(outcome=ok, only_kw_a=True, returned=4)   # and a valid one constructs

    def test_healthy_fixture_passes_every_gate(self):
        for name, g in self.V.GATES.items():
            v = self.V.judge(name, _healthy(g.scope))
            self.assertEqual(v.status, "PASS", (name, v.reason))

    def test_generated_invalid_evidence_is_never_pass_and_never_raises(self):
        problems, n = [], 0
        for name, g in self.V.GATES.items():
            full = _healthy(g.scope)
            base = {k: full[k] for k in {f.path[0] for f in g.evidence.SPEC.values()}}   # only what the gate's evidence reads
            cases = []
            for fname, path, f in self.leaves(g.evidence, ()):
                healthy = summarize.fetch(base, path)
                parent = summarize.fetch(base, path[:-1])
                body = parent.get(f.text) if f.text and isinstance(parent, dict) else None
                cases += [("%s %s" % (".".join(path), lab), {path: val}) for lab, val in self.mutations(f, healthy, body)]
                if f.text and f.expect and "LICENCE_REFUSAL" in f.expect and "AUTHORISATION_REFUSAL" not in f.expect:
                    cases.append(("%s 403 without licence text" % ".".join(path), {path[:-1] + (f.text,): {"reason": "action unauthorized for user"}}))
                if len(path) > 1:
                    cases += [("%s empty parent" % ".".join(path), {path[:-1]: {}}), ("%s parent as list" % ".".join(path), {path[:-1]: []})]
            cases += list(self.relation_mutations(g.evidence, (), base))
            for lab, changes in cases:
                R = copy.deepcopy(base)
                try:
                    for p, val in changes.items():
                        _set(R, p, val)
                except (KeyError, TypeError):
                    continue   # an earlier change removed the container
                n += 1
                try:
                    v = self.V.judge(name, R)
                except Exception as e:  # noqa: BLE001
                    problems.append("%s %s: raised %s" % (name, lab, type(e).__name__))
                    continue
                if v.status == "PASS":
                    problems.append("%s %s: PASS" % (name, lab))
                elif not v.reason:
                    problems.append("%s %s: %s without a reason" % (name, lab, v.status))
        self.assertGreater(n, 2000)
        self.assertEqual(problems, [], "\n".join(problems[:60]))

    def test_every_field_has_a_domain_and_every_relation_names_declared_fields(self):
        for name, g in self.V.GATES.items():
            stack = [g.evidence]
            while stack:
                c = stack.pop()
                for fname, f in c.SPEC.items():
                    if isinstance(f.kind, type):
                        self.assertTrue(issubclass(f.kind, self.E.Evidence), (c, fname))
                        stack.append(f.kind)
                    else:
                        self.assertIn(f.kind, self.E.KINDS, (c, fname))
                        if self.E.KINDS[f.kind].type == "status":
                            self.assertTrue(f.expect, (c, fname, "an outcome field declares its intended cases"))
                for rel in c.RELATIONS:
                    names = [x for part in rel[1:] for x in (part if isinstance(part, tuple) else (part,))]
                    for x in names:
                        head, _, attr = x.partition(".")
                        self.assertIn(head, c.SPEC, (c, rel))
                        if attr:   # one level into nested evidence
                            self.assertIn(attr, c.SPEC[head].kind.SPEC, (c, rel))

    def test_verdicts_see_only_evidence(self):
        """Structural: every verdict function takes one evidence object; verdicts.py never subscripts life data by key and never compares
        a bare HTTP status."""
        for name, g in self.V.GATES.items():
            params = list(inspect.signature(g.verdict).parameters.values())
            self.assertEqual(len(params), 1, name)
            self.assertIs(params[0].annotation, g.evidence, name)
        tree = ast.parse(open(os.path.join(LIC, "verdicts.py")).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                sl = node.slice.value if isinstance(node.slice, ast.Index) else node.slice
                self.assertFalse(isinstance(sl, ast.Constant) and isinstance(sl.value, str), ast.dump(node)[:120])
            if isinstance(node, ast.Compare):
                for c in [node.left] + node.comparators:
                    self.assertFalse(isinstance(c, ast.Constant) and type(c.value) is int and 100 <= c.value <= 599, ast.dump(node)[:120])

    def test_no_gate_status_reported_outside_the_table(self):
        outs = {g.out for g in self.V.GATES.values()}
        R = _healthy("down")
        reps = [summarize.exp1_checks(R), summarize.exp2_checks(R, "down"), summarize.exp3_checks(R), summarize.exp3_checks(R)["migration_checks"],
                summarize.exp2_checks(_healthy("up"), "up")]
        for rep in reps:
            for k in rep:
                if k.endswith("_status") or k.endswith("_pass") or k.endswith("_ok"):
                    self.assertIn(k.rsplit("_", 1)[0], outs, k)

# ---- gate review 9 (F17 to F20) -------------------------------------------------------------------------------------------------
LIC_BODY = (b'{"error":{"root_cause":[{"type":"security_exception","reason":"current license is non-compliant for [rrf]",'
            b'"license.expired.feature":"rrf"}],"type":"security_exception","reason":"current license is non-compliant for [rrf]",'
            b'"license.expired.feature":"rrf"},"status":403}')


def adapter_message(code, raw):
    """The message the REAL adapter builds for an HTTP error (ElasticsearchSearch._req with a fake urlopen)."""
    import io
    import urllib.error
    from memcore.adapters import elasticsearch_search as es
    from memcore.ports import BackendError

    def fake(req, timeout=None, context=None):
        raise urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(raw))
    a = es.ElasticsearchSearch("http://127.0.0.1:1", lambda: "x", "t")
    old, es._urlopen = es._urlopen, fake
    try:
        a._req("POST", "/x/_search", {})
    except BackendError as e:
        return str(e)
    finally:
        es._urlopen = old
    raise AssertionError("adapter did not raise")


def _detects(msg):
    from memcore.adapters.elasticsearch_search import ElasticsearchSearch
    from memcore.ports import BackendError
    return ElasticsearchSearch._is_licence_error(BackendError(msg))


class RawEdgeTests(unittest.TestCase):
    """Gate review 9: the typed layer starts at the raw row (F17); EXP1-T3 is the adapter's own predicate (F18); the current summary
    artefact (F19); evidence owns its payloads (F20)."""

    # ---- F17: raw rows, through summarize.load on a temporary copy of down_7 -----------------------------------------------------
    @classmethod
    def setUpClass(cls):
        import gzip
        cls.POLL = json.load(gzip.open(os.path.join(RES, "down_7.poll.gz"), "rt"))
        cls.T_RESP = json.load(open(os.path.join(RES, "down_7.json")))["start_basic"]["t_resp"]

    def load_with(self, poll=None, rows=None, life=None, drop=()):
        import gzip
        import shutil
        d = tempfile.mkdtemp()
        try:
            R = json.load(open(os.path.join(RES, "down_7.json"))) if life is None else life
            json.dump(R, open(os.path.join(d, "down_7.json"), "w"))
            if "poll" not in drop:
                with gzip.open(os.path.join(d, "down_7.poll.gz"), "wt") as fh:
                    json.dump(self.POLL if poll is None else poll, fh)
            if "rows" not in drop:
                if rows is None:
                    shutil.copy(os.path.join(RES, "down_7.rows.gz"), d)
                else:
                    with gzip.open(os.path.join(d, "down_7.rows.gz"), "wt") as fh:
                        fh.write("".join(json.dumps(r) + "\n" for r in rows))
            return summarize.load(d, "down")[0]
        finally:
            shutil.rmtree(d)

    def late(self):
        """Index of a poll after the first rrf refusal."""
        return next(i for i, p in enumerate(self.POLL) if p["rrf"][0] == "LICENCE_ERROR") + 20

    def test_f17_healthy_raw_poll_passes(self):
        self.assertEqual(summarize.exp1_checks(self.load_with())["T2_status"], "PASS")

    def test_f17_late_success_with_a_bad_timestamp_is_not_a_pass(self):
        i = self.late()
        for bad in (float("nan"), -1e300, float("inf"), "x", None, "absent", 1e12):
            P = copy.deepcopy(self.POLL)
            P[i]["rrf"] = ["AVAILABLE", 200]
            if bad == "absent":
                del P[i]["_t1"]
            else:
                P[i]["_t1"] = bad
            self.assertNotEqual(summarize.exp1_checks(self.load_with(poll=P))["T2_status"], "PASS", bad)

    def test_f17_raw_poll_mutations_are_unverified(self):
        i = self.late()
        muts = {
            "out of order": lambda P: P.__setitem__(slice(i, i + 2), [P[i + 1], P[i]]),
            "duplicated row": lambda P: P.insert(i, copy.deepcopy(P[i])),
            "overlapping polls": lambda P: P[i].__setitem__("_t0", P[i - 1]["_t1"] - 0.01),
            "t0 after t1": lambda P: P[i].__setitem__("_t0", P[i]["_t1"] + 0.01),
            "negative time": lambda P: P[0].__setitem__("_t0", -1.0),
            "far future": lambda P: P[-1].__setitem__("_t1", 1e12),
            "before the run": lambda P: P.insert(0, dict(P[0], _t0=1.0, _t1=2.0)),
            "cell wrong type": lambda P: P[i].__setitem__("rrf", "LICENCE_ERROR"),
            "cell label contradicts status": lambda P: P[i].__setitem__("rrf", ["AVAILABLE", 403]),
            "unknown label": lambda P: P[i].__setitem__("rrf", ["MAYBE", 403]),
            "row wrong type": lambda P: P.insert(i, ["x"]),
        }
        for lab, m in muts.items():
            P = copy.deepcopy(self.POLL)
            m(P)
            self.assertEqual(summarize.exp1_checks(self.load_with(poll=P))["T2_status"], "UNVERIFIED", lab)

    def test_f17_raw_workload_row_mutations_are_unverified(self):
        """The generator's raw-row mutations on rows.gz: every one makes EXP2-T2, T3 and T5 UNVERIFIED (never PASS, never raises)."""
        import gzip
        base = [json.loads(l) for l in gzip.open(os.path.join(RES, "down_7.rows.gz"), "rt")]
        k = next(j for j in range(len(base) // 2, len(base) - 1) if base[j][1] < base[j + 1][1])

        def at(j, pos, v):
            return lambda W: W[j].__setitem__(pos, v)
        muts = {
            "t1 NaN": at(k, 1, float("nan")), "t0 +inf": at(k, 0, float("inf")), "t1 -inf": at(k, 1, float("-inf")),
            "t0 negative": at(k, 0, -1.0), "t1 far future": at(-1, 1, 1e12), "t0 before the run": at(0, 0, 2.0),
            "t0 after t1": at(k, 0, base[k][1] + 0.5), "t1 string": at(k, 1, "1794859121.5"), "t0 bool": at(k, 0, True),
            "t0 huge int": at(k, 0, 10 ** 400),
            "out of order": lambda W: W.__setitem__(slice(k, k + 2), [W[k + 1], W[k]]),
            "duplicated": lambda W: W.insert(k, copy.deepcopy(W[k])),
            "row wrong type": lambda W: W.__setitem__(k, {"t0": W[k][0]}),
            "field missing": lambda W: W.__setitem__(k, W[k][:7]),
            "ok wrong type": at(k, 4, "1"), "ok out of domain": at(k, 4, 2), "unknown arm": at(k, 2, "auto2"),
            "ok with error text": lambda W: W[k].__setitem__(7, "HTTP 403: x") if W[k][4] else W[k].__setitem__(4, 1),
            "failure without error text": lambda W: W.__setitem__(k, W[k][:4] + [0] + W[k][5:7] + [None]),
        }
        for lab, m in muts.items():
            W = copy.deepcopy(base)
            m(W)
            r = summarize.exp2_checks(self.load_with(rows=W), "down")
            self.assertEqual((r["T2_status"], r["T3_status"], r["T5_status"]), ("UNVERIFIED",) * 3, lab)

    def test_f17_a_refusal_of_the_wrong_kind_starts_the_no_success_window(self):
        """[403 without the licence text, 200, 403 licence refusal] after the transition: the probe answered after being refused."""
        P = copy.deepcopy(self.POLL)
        i = next(k for k, p in enumerate(P) if p["rrf"][0] == "LICENCE_ERROR")
        P[i]["rrf"], P[i + 1]["rrf"] = ["OTHER_ERROR", 403], ["AVAILABLE", 200]
        self.assertNotEqual(summarize.exp1_checks(self.load_with(poll=P))["T2_status"], "PASS")

    def test_f17_a_stored_summary_is_never_accepted_without_its_raw_file(self):
        R = self.load_with()
        life = json.load(open(os.path.join(RES, "down_7.json")))
        life["exp1_moment"] = copy.deepcopy(R["exp1_moment"])            # carries first_refusal objects
        life["exp2"]["T5_control_after_1s"]["licence_refusals"] = R["exp2"]["T5_control_after_1s"]["licence_refusals"]
        L = self.load_with(life=life, drop=("poll", "rows"))
        self.assertEqual(summarize.exp1_checks(L)["T2_status"], "UNVERIFIED")
        self.assertEqual(summarize.exp2_checks(L, "down")["T5_status"], "UNVERIFIED")

    def test_f17_stored_availability_and_flip_must_agree_with_the_raw_rows(self):
        import gzip
        rows = [json.loads(l) for l in gzip.open(os.path.join(RES, "down_7.rows.gz"), "rt")]
        k = next(j for j, r in enumerate(rows) if r[2] == "auto" and r[4] and r[0] > self.T_RESP + 10)
        rows[k] = rows[k][:4] + [False, rows[k][5], [], "BackendError: HTTP 503: unavailable"]
        L = self.load_with(rows=rows)
        self.assertNotEqual(summarize.exp2_checks(L, "down")["T2_status"], "PASS")
        rows = [json.loads(l) for l in gzip.open(os.path.join(RES, "down_7.rows.gz"), "rt")]
        rows[k][1] = float("nan")
        L = self.load_with(rows=rows)
        r = summarize.exp2_checks(L, "down")
        self.assertEqual((r["T2_status"], r["T3_status"], r["T5_status"]), ("UNVERIFIED",) * 3)

    # ---- F18: EXP1-T3 is the adapter's detector on the adapter's message ----------------------------------------------------------
    def latch_life(self, body=LIC_BODY, code=403, error=None, recorded=None, status=None):
        R = _life("down_7")
        msg = adapter_message(code, body)
        for c in ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate"):
            cell = R["exp1_after"][c]
            cell["status"] = code if status is None else status
            cell["adapter_message"] = msg
            cell["adapter_detects"] = _detects(msg) if recorded is None else recorded
            if error is not None:
                cell["error"] = error
        return R

    def t3(self, R):
        return summarize.exp1_checks(R)["T3_latch_status"]

    def test_f18_adapter_message_format_is_the_adapters(self):
        import observe
        for body in (LIC_BODY, b"x" * 1000, ("é" * 300).encode("utf-8"), b""):
            self.assertEqual(observe.adapter_message(403, body), adapter_message(403, body))

    def test_f18_t3_passes_on_the_adapter_detector(self):
        self.assertEqual(self.t3(self.latch_life()), "PASS")

    def test_f18_generic_licence_refusal_but_adapter_false_is_not_a_pass(self):
        body = b'{"error":{"type":"security_exception","reason":"license expired"},"status":403}'
        self.assertNotEqual(self.t3(self.latch_life(body=body, error={"type": "security_exception", "reason": "license expired"})), "PASS")
        pad = b'{"error":{"type":"security_exception","note":"' + b"p" * 500 + b'","reason":"current license is non-compliant for [rrf]"}}'
        self.assertNotEqual(self.t3(self.latch_life(body=pad)), "PASS")   # the licence text lies beyond the adapter's 400 bytes

    def test_f18_adapter_true_but_generic_summary_without_the_text_passes(self):
        """The adapter's own predicate decides T3: the stored error summary does not carry the phrase, the adapter message does."""
        self.assertEqual(self.t3(self.latch_life(error={"type": "security_exception", "reason": "x", "root": "x", "feature": None})), "PASS")

    def test_f18_recorded_detector_or_status_that_contradicts_the_message_is_unverified(self):
        self.assertEqual(self.t3(self.latch_life(body=b'{"error":"other"}', recorded=True)), "UNVERIFIED")
        self.assertEqual(self.t3(self.latch_life(status=503)), "UNVERIFIED")

    def test_f18_committed_latch_cells_without_the_adapter_message_are_unverified(self):
        for n in range(1, 8):
            self.assertEqual(summarize.exp1_checks(_life("down_%d" % n))["T3_latch_status"], "UNVERIFIED", n)

    def test_f18_licence_signature_is_the_registered_one(self):
        import evidence as E
        self.assertFalse(E.licence_text({"reason": "license expired"}))
        self.assertFalse(E.licence_text({"license.expired.feature": "rrf"}))
        self.assertTrue(E.licence_text({"reason": "current license is non-compliant for [rrf]"}))

    # ---- F19: the current summary artefact ---------------------------------------------------------------------------------------
    def test_f19_current_summary_artefact_is_what_the_code_produces(self):
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        self.assertEqual(cur, json.loads(json.dumps(summarize.current_summary(RES), sort_keys=True)))
        for life, gates in cur["lives"].items():
            for g, v in gates.items():
                self.assertIn(v["status"], ("PASS", "FAIL", "UNVERIFIED"), (life, g))
                self.assertTrue(v["reason"], (life, g))

    def test_f19_historical_summaries_are_indexed_as_superseded_and_unchanged(self):
        import hashlib
        idx = json.load(open(os.path.join(RES, "SUPERSEDED.json")))
        self.assertEqual(idx["current"], "summary_current.json")
        man = {name: digest for digest, name in (l.split() for l in open(os.path.join(RES, "MANIFEST.sha256")) if l.strip())}
        names = {e["file"] for e in idx["superseded"]}
        self.assertEqual(names, {"summary_corrected.json", "summary_campaign.json", "summary_supp.json", "summary_down_7.json"})
        for e in idx["superseded"]:
            got = hashlib.sha256(open(os.path.join(RES, e["file"]), "rb").read()).hexdigest()
            self.assertEqual((e["sha256"], man[e["file"]]), (got, got), e["file"])
            self.assertEqual(e["superseded_by"], "summary_current.json")

    # ---- F20: parsed evidence owns its payloads ----------------------------------------------------------------------------------
    def test_f20_mutating_the_source_after_parsing_does_not_change_evidence(self):
        import evidence as E
        import verdicts
        R = _healthy("down")
        R.update(self.latch_life())
        for name, g in verdicts.GATES.items():
            src = copy.deepcopy(R if g.scope != "up" else _healthy("up"))
            ev, why = E.parse(g.evidence, src)
            if ev is None:
                continue
            before = repr(ev)
            stack = [src]
            while stack:   # empty every container in the source
                x = stack.pop()
                kids = list(x.values()) if isinstance(x, dict) else list(x) if isinstance(x, list) else []
                stack += [k for k in kids if isinstance(k, (dict, list))]
                if isinstance(x, (dict, list)):
                    x.clear()
            self.assertEqual(repr(ev), before, name)


class RebuildTests(unittest.TestCase):
    """Gate review 10. F21: every stored derived figure with a raw source is deleted and rebuilt from typed rows; nothing stored is read
    (completeness generated from the evidence types and summarize's declared sources). F22: every workload-row field has a declared
    domain and relation (observe.ROW_FIELDS drives the mutation generator). F23: current_summary's inventory is the campaign, not the
    files found. F24: the adapter message at the 400-byte boundary."""

    @classmethod
    def setUpClass(cls):
        import gzip
        import evidence as E
        import verdicts as V
        cls.E, cls.V = E, V
        cls.raw = {}
        for life in ("down_7", "up_1"):
            base = os.path.join(RES, life)
            raw = {"rows": [json.loads(l) for l in gzip.open(base + ".rows.gz", "rt")]}
            for k in ("poll", "oracle"):
                p = "%s.%s.gz" % (base, k)
                raw[k] = json.load(gzip.open(p, "rt")) if os.path.exists(p) else None
            cls.raw[life] = raw

    def stored(self, life):
        return json.load(open(os.path.join(RES, life + ".json")))

    def leaves(self, scope):
        """Every (gate, absolute leaf path) a verdict of this direction reads, from the evidence types."""
        out = []
        for name, g in self.V.GATES.items():
            if g.scope in ("both", scope):
                out += [(name, p) for p in self.E.leaf_paths(g.evidence)]
        return out

    def rebuilt(self, life, mutate=None):
        R, raw = self.stored(life), copy.deepcopy(self.raw[life])
        if mutate:
            mutate(raw, R)
        summarize.rebuild(R, raw)
        return R

    # ---- F21: completeness of the raw rebuild ------------------------------------------------------------------------------------
    def test_f21_every_leaf_a_verdict_reads_has_a_declared_source(self):
        for scope in ("down", "up"):
            for gate, p in self.leaves(scope):
                self.assertIn(summarize.source_of(p), set(summarize.RAW_SOURCES) | {"stored"}, (gate, p))
                if summarize.source_of(p) == "stored":
                    self.assertTrue(summarize.stored_category(p), (gate, p, "a stored leaf needs a declared category"))

    def test_f21_stored_values_of_raw_sourced_leaves_are_never_read(self):
        """Poison every raw-sourced leaf in the stored life: the rebuilt record and every verdict are unchanged."""
        for life, scope in (("down_7", "down"), ("up_1", "up")):
            clean = self.rebuilt(life)
            R = self.stored(life)
            for _, p in self.leaves(scope):
                if summarize.source_of(p) == "stored" or summarize.fetch(R, p) is summarize.MISSING:
                    continue
                v = summarize.fetch(R, p)
                bad = (not v) if type(v) is bool else v + 7919 if type(v) is int else v + 77.7 if type(v) is float else [[1.0, 9.0, 1]]
                _set(R, p, bad)
            R["exp1_moment"] = {f: {"first_refusal": {"status": 403, "licence_text": True, "rel_resp_s": 0.1}, "n": 9,
                                    "first_refusal_rel_resp_s": 0.1, "success_after_first_refusal": 0} for f in PROBES} if scope == "down" else {}
            summarize.rebuild(R, copy.deepcopy(self.raw[life]))
            for gate, p in self.leaves(scope):
                if summarize.source_of(p) != "stored":
                    self.assertEqual(summarize.fetch(R, p), summarize.fetch(clean, p), (life, p))
            for name, g in self.V.GATES.items():
                if g.scope in ("both", scope):
                    self.assertEqual(self.V.judge(name, R).status, self.V.judge(name, clean).status, (life, name))

    def raw_mutations(self, scope):
        """Raw-row mutations that each change some true figure; the coverage test requires that together they change every
        raw-sourced leaf, so a leaf still read from storage would show as never changing."""
        import oracle
        T = lambda R: (R["start_basic"] if scope == "down" else R["start_trial"])
        FAIL = lambda r: r[:4] + [0, None, None, "BackendError: HTTP 503: x"]
        def ok_row(r):
            return r[:4] + [1, "backend", ["d%05d" % i for i in range(10)], None]
        def first(W, pred):
            return next(i for i, r in enumerate(W) if pred(r))
        def last(W, pred):
            return max(i for i, r in enumerate(W) if pred(r))
        def rows(fn):
            return lambda raw, R: fn(raw["rows"], T(R)["t_req"], T(R)["t_resp"], raw)
        def drop(i):
            return lambda W, *a: W.pop(i(W, *a))
        def setrow(i, f):
            def m(W, *a):
                j = i(W, *a)
                W[j] = f(W[j], *a)
            return m
        late_auto = lambda W, q, s, *a: last(W, lambda r: r[2] == "auto" and r[4] and r[0] > s + 5)
        out = {
            "a late auto read fails": rows(setrow(late_auto, lambda r, *a: FAIL(r))),
            "one lexical read is gone": rows(drop(lambda W, *a: last(W, lambda r: r[2] == "lex"))),
            "ctl before the request: one row gone": rows(drop(lambda W, q, *a: first(W, lambda r: r[2] == "ctl" and r[1] < q))),
            "ctl after the transition: one row gone": rows(drop(lambda W, q, s, *a: last(W, lambda r: r[2] == "ctl" and r[0] >= s + 1))),
            "ctl after: a licence refusal becomes a 503": rows(setrow(
                lambda W, q, s, *a: last(W, lambda r: r[2] == "ctl" and r[0] >= s + 1 and not r[4]), lambda r, *a: FAIL(r))),
            "ctl after: a licence refusal becomes a success": rows(setrow(
                lambda W, q, s, *a: last(W, lambda r: r[2] == "ctl" and r[0] >= s + 1 and not r[4]), lambda r, *a: ok_row(r))),
            "ctl after: a success becomes a 503 (F21)": rows(setrow(
                lambda W, q, s, *a: last(W, lambda r: r[2] == "ctl" and r[0] >= s + 1 and r[4]), lambda r, *a: FAIL(r))),
            "ctl before: a licence refusal becomes a 503": rows(setrow(
                lambda W, q, *a: first(W, lambda r: r[2] == "ctl" and r[1] < q and not r[4]), lambda r, *a: FAIL(r))),
            "ctl before: a licence refusal becomes a success": rows(setrow(
                lambda W, q, *a: first(W, lambda r: r[2] == "ctl" and r[1] < q and not r[4]), lambda r, *a: ok_row(r))),
        }
        if scope == "down":
            out["the first client answer is relabelled backend"] = rows(setrow(
                lambda W, q, *a: first(W, lambda r: r[2] == "auto" and r[4] and r[5] == "client" and r[1] >= q), lambda r, *a: r[:5] + ["backend"] + r[6:]))
            out["a late client answer is relabelled backend"] = rows(setrow(
                lambda W, q, s, *a: last(W, lambda r: r[2] == "auto" and r[4] and r[5] == "client"), lambda r, *a: r[:5] + ["backend"] + r[6:]))
            out["a client answer returns unrelated ids"] = rows(setrow(
                lambda W, *a: last(W, lambda r: r[2] == "auto" and r[4] and r[5] == "client"), lambda r, *a: r[:6] + [["nope"]] + r[7:]))
            out["a backend answer has no reference"] = rows(setrow(
                lambda W, *a: first(W, lambda r: r[2] == "auto" and r[4] and r[5] == "backend"), lambda r, *a: r[:3] + [10 ** 6] + r[4:]))
            out["a broken-fallback client answer is the legal client fusion"] = rows(setrow(
                lambda W, *a: last(W, lambda r: r[2] == "brk" and r[4] and r[5] == "client"),
                lambda r, q, s, raw: r[:6] + [[d for d, _ in sorted(oracle.rrf_scores(list(raw["oracle"]["legs"][str(r[3])])).items(),
                                                                  key=lambda x: -x[1])[:10]]] + r[7:]))
            out["a broken-fallback backend answer is gone"] = rows(drop(lambda W, *a: last(W, lambda r: r[2] == "brk" and r[4] and r[5] == "backend")))
            out["a broken-fallback backend answer is wrong"] = rows(setrow(
                lambda W, *a: first(W, lambda r: r[2] == "brk" and r[4] and r[5] == "backend"), lambda r, *a: r[:6] + [["nope"]] + r[7:]))
            out["a broken-fallback client answer is gone"] = rows(drop(lambda W, *a: last(W, lambda r: r[2] == "brk" and r[4] and r[5] == "client")))

            def poll(fn):
                return lambda raw, R: fn(raw["poll"], R["start_basic"]["t_resp"])
            def each_probe(cell):
                def m(P, t):
                    for f in PROBES:
                        cell(P, t, f)
                return m
            def late_success(P, t, f):
                P[-1][f] = ["AVAILABLE", 200] + ([4] if f == "dls_search" else [])
            def first_refusal_later(P, t, f):
                i = next(k for k, p in enumerate(P) if p["_t1"] >= t - 0.5 and p[f][1] == 403)
                P[i][f] = ["OTHER_ERROR", 503] + P[i][f][2:]
            def no_refusal(P, t, f):
                for p in P:
                    if p[f][1] == 403:
                        p[f] = ["OTHER_ERROR", 503] + p[f][2:]
            out["poll: the last poll is gone"] = poll(lambda P, t: P.pop())
            out["poll: every probe answers in the last poll"] = poll(each_probe(late_success))
            out["poll: every first refusal is one poll later"] = poll(each_probe(first_refusal_later))
            out["poll: no probe is ever refused"] = poll(each_probe(no_refusal))
        else:
            out["the first backend answer is relabelled client"] = rows(setrow(
                lambda W, q, s, *a: first(W, lambda r: r[2] == "auto" and r[4] and r[5] == "backend" and r[1] >= s), lambda r, *a: r[:5] + ["client"] + r[6:]))
            out["a late backend answer is relabelled client"] = rows(setrow(
                lambda W, *a: last(W, lambda r: r[2] == "auto" and r[4] and r[5] == "backend"), lambda r, *a: r[:5] + ["client"] + r[6:]))
        return out

    def test_f21_every_raw_sourced_leaf_follows_its_raw_rows(self):
        for life, scope in (("down_7", "down"), ("up_1", "up")):
            base = self.rebuilt(life)
            stored = self.stored(life)
            want = {p for _, p in self.leaves(scope) if summarize.source_of(p) in summarize.rebuilt_sources(base)}
            changed = set()
            for lab, m in self.raw_mutations(scope).items():
                try:
                    R = self.rebuilt(life, m)
                except (StopIteration, ValueError):
                    continue   # the mutation does not apply to this life (no such row)
                for p in want:
                    if summarize.fetch(R, p) != summarize.fetch(base, p):
                        changed.add(p)
                        if summarize.fetch(stored, p) is not summarize.MISSING:
                            self.assertNotEqual(summarize.fetch(R, p), summarize.fetch(stored, p), (life, lab, p, "stored value read"))
            self.assertEqual(sorted(want - changed), [], (life, "raw-sourced leaves no raw mutation changed"))

    def test_f21_post_transition_control_failures_fail_the_up_gate(self):
        def all_post_fail(raw, R):
            s = R["start_trial"]["t_resp"]
            raw["rows"] = [r[:4] + [0, None, None, "BackendError: HTTP 503: x"] if r[2] == "ctl" and r[0] >= s + 1 else r for r in raw["rows"]]
        R = self.rebuilt("up_1", all_post_fail)
        self.assertEqual(self.V.judge("EXP2-T5-up", R).status, "FAIL")
        self.assertEqual(self.V.judge("EXP2-T5-up", self.rebuilt("up_1")).status, "PASS")

    def test_f21_a_figure_without_raw_source_says_so(self):
        R = self.rebuilt("up_1")   # no oracle legs were saved for up_1: T1 is from stored figures
        self.assertIn("from stored figure, raw not available", summarize.provenance("EXP2-T1", R))
        self.assertIn("from stored figure, raw not available", summarize.provenance("EXP2-T4", R))
        self.assertNotIn("from stored figure", summarize.provenance("EXP2-T2", R))
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        self.assertIn("from stored figure, raw not available", cur["lives"]["up_1.json"]["EXP2-T1"]["reason"])

    # ---- F22: every raw field has a declared domain -------------------------------------------------------------------------------
    BAD = {   # out-of-domain values per declared domain; a domain missing here fails the test
        "time": (float("nan"), float("inf"), -1.0, 1e12, "1794859121.5", None, True, 10 ** 400, [1.0]),
        "arm": ("auto2", 17, None, {"a": 1}, ["auto"]),
        "query_index": (-1, 1.5, "3", None, True, 10 ** 400, {"q": 1}),
        "ok": (2, -1, "1", None, 1.0, [1]),
        "label": (17, "bogus", {"x": 1}, ["client"], 1.5),
        "ids": ("d1", [1, 2], [None], ["d"] * 11, {"d": 1}, [["d"]]),
        "error_text": (17, {"e": 1}, ["x"], "no failure form", ""),
    }

    def slice_rows(self):
        """400 consecutive committed down_7 rows around the first failure (a parseable stream with successes and failures)."""
        rows = self.raw["down_7"]["rows"]
        j = next(i for i, r in enumerate(rows) if not r[4])
        W = [list(r) for r in rows[max(0, j - 200):j + 200]]
        return W, (min(r[0] for r in W) - 1, max(r[1] for r in W) + 1)

    def via_load(self, life, mutate):
        """The same mutation through summarize.load on a temporary copy (the path the committed reports use)."""
        import gzip
        import shutil
        d = tempfile.mkdtemp()
        try:
            for f in os.listdir(RES):
                if f.startswith(life + "."):
                    shutil.copy(os.path.join(RES, f), d)
            raw, R = copy.deepcopy(self.raw[life]), self.stored(life)
            mutate(raw, R)
            with gzip.open(os.path.join(d, life + ".rows.gz"), "wt") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in raw["rows"]))
            return summarize.load(d, life.split("_")[0])[0]
        finally:
            shutil.rmtree(d)

    def test_f21_post_transition_control_failures_fail_the_up_gate_through_load(self):
        def all_post_fail(raw, R):
            s = R["start_trial"]["t_resp"]
            raw["rows"] = [r[:4] + [0, None, None, "BackendError: HTTP 503: x"] if r[2] == "ctl" and r[0] >= s + 1 else r for r in raw["rows"]]
        self.assertEqual(self.V.judge("EXP2-T5-up", self.via_load("up_1", all_post_fail)).status, "FAIL")

    def test_f22_a_bad_label_is_unverified_through_load(self):
        def relabel(raw, R):
            s = R["start_basic"]["t_resp"]
            k = max(i for i, r in enumerate(raw["rows"]) if r[2] == "auto" and r[4] and r[0] > s + 5)
            raw["rows"][k][5] = 17
        self.assertEqual(self.V.judge("EXP2-T3-down", self.via_load("down_7", relabel)).status, "UNVERIFIED")

    def test_f22_every_row_field_has_a_domain_and_every_out_of_domain_value_is_unparsed(self):
        import observe
        W, window = self.slice_rows()
        observe.parse_work(W, window)
        fail_i = next(i for i, r in enumerate(W) if not r[4])
        ok_i = next(i for i, r in enumerate(W) if r[4] and r[2] == "auto")
        seen = set()
        for f in observe.ROW_FIELDS:
            self.assertIn(f.domain, self.BAD, "field %s has no declared domain with test values" % f.name)
            seen.add(f.index)
            for bad in self.BAD[f.domain]:
                for i in (ok_i, fail_i):
                    M = copy.deepcopy(W)
                    M[i][f.index] = bad
                    with self.assertRaises(observe.Unparsed, msg=(f.name, bad, i)):
                        observe.parse_work(M, window)
        self.assertEqual(seen, set(range(8)), "every position of a workload row is a declared field")

    def test_f22_label_success_and_failure_relations(self):
        import observe
        W, window = self.slice_rows()
        auto = next(i for i, r in enumerate(W) if r[4] and r[2] == "auto")
        lex = next(i for i, r in enumerate(W) if r[4] and r[2] == "lex")
        fail = next(i for i, r in enumerate(W) if not r[4])
        cases = {
            "auto success labelled none": (auto, 5, "none"), "auto success without label": (auto, 5, None),
            "lexical success labelled client": (lex, 5, "client"), "failure with a label": (fail, 5, "backend"),
            "failure with ids": (fail, 6, ["d1"]), "success with error text": (auto, 7, "BackendError: x"),
            "success without ids": (auto, 6, None),
        }
        for lab, (i, pos, v) in cases.items():
            M = copy.deepcopy(W)
            M[i][pos] = v
            with self.assertRaises(observe.Unparsed, msg=lab):
                observe.parse_work(M, window)

    def test_f22_a_bad_label_on_a_late_auto_answer_is_unverified_end_to_end(self):
        def relabel(v):
            def m(raw, R):
                s = R["start_basic"]["t_resp"]
                k = max(i for i, r in enumerate(raw["rows"]) if r[2] == "auto" and r[4] and r[0] > s + 5)
                raw["rows"][k][5] = v
            return m
        for v in (17, "bogus", {"x": 1}):
            R = self.rebuilt("down_7", relabel(v))
            self.assertEqual(self.V.judge("EXP2-T3-down", R).status, "UNVERIFIED", v)

    # ---- F23: the inventory is the campaign ---------------------------------------------------------------------------------------
    def test_f23_empty_input_is_unverified_for_every_gate(self):
        d = tempfile.mkdtemp()
        try:
            cur = summarize.current_summary(d)
        finally:
            os.rmdir(d)
        self.assertEqual(set(cur["aggregate"]), set(self.V.GATES))
        for g, v in cur["aggregate"].items():
            self.assertEqual(v["status"], "UNVERIFIED", g)
        expected = {"%s_%d.json" % (m, n) for m, ns in summarize.CAMPAIGN.items() for n in ns}
        self.assertEqual(set(cur["lives"]), expected)
        for life, gates in cur["lives"].items():
            for g, v in gates.items():
                self.assertEqual(v["status"], "UNVERIFIED", (life, g))
                self.assertIn("absent", v["reason"])

    def test_f23_a_missing_direction_or_life_is_unverified(self):
        import shutil
        for drop in ("up", "down_3"):
            d = tempfile.mkdtemp()
            try:
                for f in os.listdir(RES):
                    if f.startswith(drop + "_" if drop == "up" else drop + "."):
                        continue
                    if f.startswith(("down_", "up_")):
                        shutil.copy(os.path.join(RES, f), d)
                cur = summarize.current_summary(d)
            finally:
                shutil.rmtree(d)
            for g in ("EXP2-T2", "EXP2-T3-down" if drop == "down_3" else "EXP2-T3-up", "EXP2-T5-down" if drop == "down_3" else "EXP2-T5-up"):
                self.assertEqual(cur["aggregate"][g]["status"], "UNVERIFIED", (drop, g))
                self.assertIn("absent", cur["aggregate"][g]["reason"], (drop, g))

    def test_f23_the_campaign_is_the_registered_one(self):
        self.assertEqual(summarize.CAMPAIGN, {"down": (1, 2, 3, 4, 5, 6, 7), "up": (1, 2, 3, 4, 5)})

    # ---- F24 and the disclosed limits --------------------------------------------------------------------------------------------
    def test_f24_a_full_400_byte_licence_body_passes(self):
        import observe
        body = (b'{"error":{"reason":"current license is non-compliant for [rrf]"' + b" " * 600)[:1000]
        msg = observe.adapter_message(403, body)
        self.assertEqual(len(msg), 410)
        self.assertTrue(observe.adapter_detects(msg))
        R = _healthy("down")
        for c in LATCH_CELLS:
            R["exp1_after"][c].update(adapter_message=msg, adapter_detects=True)
        self.assertEqual(self.V.judge("EXP1-T3", R).status, "PASS")

    LIMITS = ("EXP3-T2", "EXP1-T3", "EXP2-T2", "EXP1-T2", "EXP1-T1", "EXP4-T2")

    def test_disclosed_limits_are_stated_where_results_are_presented(self):
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        text = json.dumps(cur["limits"])
        for key in self.LIMITS:
            self.assertIn(key, text)
        for doc in ("DEGRADE.md", os.path.join("results", "README.md")):
            body = open(os.path.join(LIC, doc)).read()
            self.assertIn("Disclosed limits", body, doc)
            for key in self.LIMITS:
                self.assertIn(key, body, (doc, key))
        import verdicts
        self.assertNotIn("Thresholds are THRESHOLDS.md's.", verdicts.__doc__)



if __name__ == "__main__":
    unittest.main()
