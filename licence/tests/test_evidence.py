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
        row = {"_t1": t}
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
        self.assertIsNone(observe.outcome_from_cell(["AVAILABLE", 403]))      # label contradicts status: not an observation
        self.assertIsNone(observe.outcome_from_cell(["OTHER_ERROR", 200]))
        self.assertIsNone(observe.outcome_from_cell(["LICENCE_ERROR", 200]))
        for cell in (["EMPTY_WITNESS", 200], ["OTHER_ERROR", 200], ["AVAILABLE", 200]):   # any 2xx answer after the refusal
            rows = _poll({})
            rows[-1]["rrf"] = cell
            m = observe.moments(rows, T_RESP)
            self.assertEqual(m["rrf"]["success_after_first_refusal"], 1, cell)
        rows = _poll({"dls_search": ("AVAILABLE", 403)})
        self.assertIsNone(observe.moments(rows, T_RESP)["dls_search"]["first_refusal"])

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
            E.LatchCell(outcome=Sub(403, "LICENCE_REFUSAL", True), error="x")

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
            E.LatchCell(outcome=403, error="x")   # a bare status is not an outcome
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
        self.assertEqual((r["T2_status"], r["T3_latch_status"], r["T5_status"]), ("UNVERIFIED",) * 3)

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


if __name__ == "__main__":
    unittest.main()
