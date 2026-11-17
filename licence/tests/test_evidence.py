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
        out = [("None", None), ("wrong type", 123 if k.type == "text" else "x"), ("bool", True if k.type != "bool" else 1)]
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
        get = lambda n: summarize.fetch(R, absp[n])
        for rel in ecls.RELATIONS:
            kind, a = rel[0], rel[1]
            if kind == "le":
                yield "%s.%s > %s" % (ecls.__name__, a, rel[2]), {absp[a]: get(rel[2]) + 1}
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
                        self.assertIn(x, c.SPEC, (c, rel))

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


if __name__ == "__main__":
    unittest.main()
