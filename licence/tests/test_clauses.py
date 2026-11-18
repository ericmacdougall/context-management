"""Gate review 12: tests generated from the REGISTERED RULES (THRESHOLDS.md), not from the leaves the verdicts read.

licence/clauses.py is the clause table: one row per atomic THRESHOLDS.md clause with its line and quote, its kind, the gates that
decide it, the evidence fields that witness it, the recorder that produces them and the verdict that reads them. From that table:
- every bullet of THRESHOLDS.md is cited by a row, and every quote is on its cited line (completeness starts from the rules);
- every FAIL clause has a mutation, and making the clause true moves its gate to the declared non-PASS status;
- removing or corrupting any witness of a PASS/CONTROL/SUBSTITUTE clause makes the gate's evidence fail to parse (UNVERIFIED);
- a LIMIT clause (registered wording no committed field witnesses) forces its gate off PASS;
- a gate no clause cites, or a clause no test reaches, fails.
Also F28 (the DLS recorder must see a document whatever the HTTP status), the historical restatement, F29 (no derivation survives its
raw target changing) and F30 (only the current-report writer may write summary_current.json).
Run: cd licence && python3 -m unittest tests.test_clauses
"""
import collections
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIC = os.path.dirname(HERE)
RES = os.path.join(LIC, "results")
sys.path.insert(0, LIC)
sys.path.insert(0, os.path.dirname(LIC))
import evidence as E  # noqa: E402
import summarize  # noqa: E402
import verdicts  # noqa: E402

try:
    import clauses  # noqa: E402
except ImportError:   # red on d8aa93d: the table does not exist; every test below then fails by assertion, not by error
    clauses = None

from tests.test_evidence import _healthy  # noqa: E402

THRESHOLDS = os.path.join(LIC, "THRESHOLDS.md")
CLAUSE_LINE_PREFIXES = ("- ",)   # every bullet of THRESHOLDS.md states a rule
EXTRA_CLAUSE_LINES = (50,)       # the "Evidence rules" paragraph is a rule but not a bullet


def _get(R, path):
    d = R
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return E.MISSING
        d = d[k]
    return d


def _put(R, path, v):
    ks = path.split(".")
    d = R
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    if v is clauses_absent():
        d.pop(ks[-1], None)
    else:
        d[ks[-1]] = v


_ABSENT = object()


def clauses_absent():
    return getattr(clauses, "ABSENT", _ABSENT) if clauses is not None else _ABSENT


def base(gate):
    """A life on which `gate`'s evidence parses and the verdict is not FAIL: the healthy fixture of its scope, plus what the
    review-12 recorder records (clauses.augment_base) for evidence the committed lives were recorded without."""
    g = verdicts.GATES[gate]
    R = _healthy("up" if g.scope == "up" else "down")
    if clauses is not None and hasattr(clauses, "augment_base"):
        clauses.augment_base(R)
    return R


class ClauseTableTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(clauses, "no clause table (licence/clauses.py): the registered rules have no witness map")

    def test_every_rule_line_of_thresholds_is_cited_and_every_quote_is_on_its_line(self):
        lines = open(THRESHOLDS).read().split("\n")
        rule_lines = {i + 1 for i, l in enumerate(lines) if l.startswith(CLAUSE_LINE_PREFIXES)} | set(EXTRA_CLAUSE_LINES)
        cited = {c.line for c in clauses.CLAUSES}
        self.assertEqual(sorted(rule_lines - cited), [], "THRESHOLDS.md rule lines no clause row cites")
        for c in clauses.CLAUSES:
            self.assertIn(c.quote, lines[c.line - 1], (c.cid, c.line))
            self.assertIn(c.kind, clauses.KINDS, c.cid)
        self.assertEqual(len({c.cid for c in clauses.CLAUSES}), len(clauses.CLAUSES), "duplicate clause ids")

    def test_every_gate_is_cited_and_every_gated_clause_names_its_recorder_and_verdict(self):
        cited = {g for c in clauses.CLAUSES for g in c.gates}
        self.assertEqual(sorted(set(verdicts.GATES) - cited), [], "gates no registered clause justifies")
        for c in clauses.CLAUSES:
            for g in c.gates:
                self.assertIn(g, verdicts.GATES, c.cid)
            if c.gates:
                fn = c.verdict.split(":")[1]
                self.assertTrue(callable(getattr(verdicts, fn, None)), (c.cid, c.verdict))
                f, name = c.recorder.split(":")
                self.assertIn("def %s(" % name, open(os.path.join(LIC, f)).read(), (c.cid, c.recorder))
            else:
                self.assertIn(c.kind, ("REPORTED", "RULE", "LIMIT"), (c.cid, "an ungated clause can only be reported, a rule or a limit"))

    def test_every_fail_clause_has_a_mutation_and_every_pass_clause_a_witness(self):
        for c in clauses.CLAUSES:
            if c.kind == "FAIL":
                self.assertTrue(c.mutations, (c.cid, "a FAIL clause with no mutation"))
                self.assertEqual({m.gate for m in c.mutations} - set(c.gates), set(), c.cid)
                self.assertEqual(set(c.gates) - {m.gate for m in c.mutations}, set(), (c.cid, "a gate of the clause no mutation reaches"))
            if c.kind in ("PASS", "CONTROL", "SUBSTITUTE"):
                self.assertTrue(c.gates and c.witness, (c.cid, "a PASS clause with no gate or no witness field"))

    def test_bases_parse_and_do_not_fail(self):
        for name in verdicts.GATES:
            R = base(name)
            ev, why = E.parse(verdicts.GATES[name].evidence, R)
            self.assertIsNotNone(ev, (name, why))
            self.assertNotEqual(verdicts.judge(name, R).status, "FAIL", name)

    def test_fail_clause_mutations_move_the_gate_off_pass_to_the_declared_status(self):
        n = 0
        for c in clauses.CLAUSES:
            for m in c.mutations:
                R = base(m.gate)
                for path, v in m.edits:
                    _put(R, path, v)
                got = verdicts.judge(m.gate, R)
                self.assertEqual(got.status, m.expect, (c.cid, m.gate, m.edits, got.reason))
                n += 1
        self.assertGreaterEqual(n, 25, "too few mutations for the registered FAIL clauses")

    def test_removing_or_corrupting_any_witness_makes_the_gate_unverified(self):
        for c in clauses.CLAUSES:
            if c.kind not in ("PASS", "CONTROL", "SUBSTITUTE"):
                continue
            for g in c.gates:
                leaves = {".".join(p) for p in E.leaf_paths(verdicts.GATES[g].evidence)}
                for w in c.witness:
                    self.assertTrue(any(l == w or l.startswith(w + ".") for l in leaves), (c.cid, g, w, "witness the gate never reads"))
                    for bad in (clauses_absent(), None, "x", -1):
                        R = base(g)
                        if _get(R, w) is E.MISSING:
                            continue
                        _put(R, w, bad)
                        if bad in (-1,) and isinstance(_get(base(g), w), bool):
                            continue
                        # UNVERIFIED either because the evidence no longer parses or because the verdict finds no witness (an
                        # optional witness whose absence the verdict must treat as "not shown", e.g. docs_in_payload)
                        self.assertEqual(verdicts.judge(g, R).status, "UNVERIFIED", (c.cid, g, w, bad))

    def test_a_limit_clause_forces_its_gate_off_pass(self):
        limited = [c for c in clauses.CLAUSES if c.kind == "LIMIT" and c.gates]
        self.assertTrue(limited)
        for c in limited:
            for g in c.gates:
                R = base(g)
                v = verdicts.judge(g, R)
                self.assertEqual(v.status, "UNVERIFIED", (c.cid, g, v.reason))
                self.assertIn(c.cid, v.reason, (c.cid, g, "the forcing limit is not named"))

    def test_current_summary_reports_every_clause_with_a_status(self):
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        self.assertIn("clauses", cur)
        self.assertEqual(sorted(cur["clauses"]), sorted(c.cid for c in clauses.CLAUSES))
        for cid, v in cur["clauses"].items():
            self.assertIn(v["status"], ("PASS", "FAIL", "UNVERIFIED"), cid)
            self.assertTrue(v["reason"], cid)
        for c in clauses.CLAUSES:
            if not c.gates:
                self.assertEqual(cur["clauses"][c.cid]["status"], "UNVERIFIED", (c.cid, "an ungated clause is never PASS"))


# ---- F28: the recorder sees a document whatever the HTTP status ------------------------------------------------------------------
DOC = {"kw": "a", "title": "allowed document"}
PAYLOADS_WITH_A_DOCUMENT = {   # one per read path shape (search, get, mget, msearch, esql, termvectors, count)
    "hits": {"hits": {"total": {"value": 1}, "hits": [{"_id": "0", "_source": DOC}]}},
    "source": {"_source": DOC},
    "found": {"_id": "0", "found": True, "_source": DOC},
    "mget": {"docs": [{"_id": "0", "found": True, "_source": DOC}]},
    "msearch": {"responses": [{"hits": {"hits": [{"_id": "0", "_source": DOC}]}}]},
    "esql": {"columns": [{"name": "title"}], "values": [["allowed document"]]},
    "termvectors": {"_id": "1", "found": True, "term_vectors": {"title": {"terms": {"berlin": {}}}}},
    "count": {"count": 4},
    "error_and_hits": {"error": {"type": "security_exception", "reason": "unauthorized"}, "status": 403,
                       "hits": {"hits": [{"_id": "0", "_source": DOC}]}},
}
REFUSAL = {"error": {"root_cause": [{"type": "security_exception", "reason": "action is unauthorized for user"}],
                     "type": "security_exception", "reason": "action is unauthorized for user"}, "status": 403}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        import lib
        import matrix
        self.m, self.lib = matrix, lib
        self.saved = (matrix.req, matrix.check_shared_base)
        matrix.check_shared_base = lambda: None

    def tearDown(self):
        self.m.req, self.m.check_shared_base = self.saved

    def stub(self, code, body):
        self.m.req = lambda *a, **kw: (code, copy.deepcopy(body), 1.0)

    def test_f28_docs_in_payload_counts_documents_by_shape_whatever_the_status(self):
        f = getattr(self.m, "docs_in_payload", None)
        self.assertTrue(callable(f), "matrix.docs_in_payload missing: the recorder cannot see a document returned with a non-2xx status")
        for name, body in PAYLOADS_WITH_A_DOCUMENT.items():
            self.assertGreater(f(body), 0, name)
        self.assertEqual(f(REFUSAL), 0)
        self.assertIsNone(f({"_raw": "<html>proxy error</html>"}), "a body of unknown shape must be unknown, never 0")
        self.assertIsNone(f(None))

    def test_f28_battery_with_a_document_in_a_403_is_a_fail(self):
        for name, body in PAYLOADS_WITH_A_DOCUMENT.items():
            self.stub(403, body)
            after = self.m.dls_battery()
            R = base("EXP1-T4-battery")
            R["dls_battery_after"] = after
            v = verdicts.judge("EXP1-T4-battery", R)
            self.assertEqual(v.status, "FAIL", (name, v.reason))

    def test_f28_battery_refused_with_nothing_returned_can_pass_and_unknown_shape_is_unverified(self):
        self.stub(403, REFUSAL)
        R = base("EXP1-T4-battery")
        R["dls_battery_after"] = self.m.dls_battery()
        self.assertEqual(verdicts.judge("EXP1-T4-battery", R).status, "PASS")
        self.stub(403, {"_raw": "not json"})
        R["dls_battery_after"] = self.m.dls_battery()
        self.assertEqual(verdicts.judge("EXP1-T4-battery", R).status, "UNVERIFIED")

    def after_cell(self, code, body):
        self.stub(code, body)
        c, j, ids, extra = self.m.dls_search()
        spec = self.m.CELLS[("run", "dls_search_as_user")]
        return self.m.verdict("run", "dls_search_as_user", {p: True for p in spec.prereqs}, c, j, ids, extra)

    def test_f28_dls_search_with_a_document_in_a_403_is_a_fail(self):
        for name, body in PAYLOADS_WITH_A_DOCUMENT.items():
            if name in ("count", "esql", "termvectors", "mget"):
                continue   # not a _search answer shape
            R = base("EXP1-T4")
            R["exp1_after"]["dls_search_as_user"] = self.after_cell(403, body)
            v = verdicts.judge("EXP1-T4", R)
            self.assertEqual(v.status, "FAIL", (name, v.reason))

    def test_f28_a_positive_post_transition_returned_count_is_a_fail(self):
        R = base("EXP1-T4")
        R["exp1_after"]["dls_search_as_user"]["returned"] = 8
        self.assertEqual(verdicts.judge("EXP1-T4", R).status, "FAIL")
        R = _healthy("down")   # the committed record, as the reviewer reproduced it
        R["exp1_after"]["dls_search_as_user"]["returned"] = 8
        self.assertEqual(verdicts.judge("EXP1-T4", R).status, "FAIL")

    def test_f28_refused_dls_search_with_nothing_returned_can_pass(self):
        R = base("EXP1-T4")
        R["exp1_after"]["dls_search_as_user"] = self.after_cell(403, REFUSAL)
        self.assertEqual(verdicts.judge("EXP1-T4", R).status, "PASS")

    def test_f28_polled_dls_probe_rows_carry_the_document_count(self):
        import observe
        self.stub(403, PAYLOADS_WITH_A_DOCUMENT["error_and_hits"])
        self.m.embed = getattr(self.m, "embed")
        est = {"shared_base": True, "dls_principal": True}
        saved = self.m.req
        try:
            row = self.m.quick_probes(est=est)
        finally:
            self.m.req = saved
        self.assertGreaterEqual(len(row["dls_search"]), 4, "the poll row does not carry the document count")
        self.assertGreater(row["dls_search"][3], 0)
        t = row["_t"]
        rows = [dict(row, _t0=t + 1, _t1=t + 2)]
        m = observe.moments(rows, t, window=None)
        self.assertGreater(m["dls_search"].get("docs_after_transition") or 0, 0)


# ---- the historical restatement ------------------------------------------------------------------------------------------------
class HistoryTests(unittest.TestCase):
    def test_committed_dls_and_migration_passes_without_a_witness_are_restated(self):
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        for life, gates in cur["lives"].items():
            for g in ("EXP1-T4", "EXP1-T4-battery", "EXP3-migration"):
                if g in gates:
                    self.assertNotEqual(gates[g]["status"], "PASS", (life, g, "a PASS with no committed witness of its registered clause"))

    def test_every_restated_status_is_listed_with_its_count(self):
        idx = json.load(open(os.path.join(RES, "SUPERSEDED.json")))
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        readme = open(os.path.join(RES, "README.md")).read()
        for review in (12, 13):
            ch = idx.get("status_changes_review_%d" % review)
            self.assertIsInstance(ch, dict, "SUPERSEDED.json does not list the review-%d status changes" % review)
            self.assertEqual(ch["count"], len(ch["changes"]))
            for e in ch["changes"]:
                where = cur["aggregate"] if e["life"] == "aggregate" else cur["lives"][e["life"]]
                self.assertEqual(where[e["gate"]]["status"], e["to"], e)
            self.assertIn("%d statuses changed" % ch["count"], readme)
        ch = idx["status_changes_review_13"]
        self.assertEqual(ch["clause_count"], len(ch["clause_changes"]))
        for e in ch["clause_changes"]:
            self.assertEqual(cur["clauses"][e["clause"]]["status"], e["to"], e)


# ---- F29: no derivation survives its raw target changing ---------------------------------------------------------------------
class CacheTests(unittest.TestCase):
    def test_f29_a_raw_symlink_whose_target_changes_between_two_reports_is_reread(self):
        d = tempfile.mkdtemp()
        tgt = tempfile.mkdtemp()
        try:
            for f in os.listdir(RES):
                if f.startswith("up_1."):
                    if f.endswith(".gz"):
                        shutil.copy(os.path.join(RES, f), os.path.join(tgt, f))
                        os.symlink(os.path.join(tgt, f), os.path.join(d, f))
                    else:
                        shutil.copy(os.path.join(RES, f), d)
            first = summarize.current_summary(d)["lives"]["up_1.json"]["EXP2-T2"]["status"]
            self.assertEqual(first, "PASS")
            with open(os.path.join(tgt, "up_1.rows.gz"), "wb") as fh:   # same link, new target content: unusable
                fh.write(b"not gzip")
            second = summarize.current_summary(d)["lives"]["up_1.json"]["EXP2-T2"]["status"]
            self.assertEqual(second, "UNVERIFIED", "a cached derivation outlived its raw target")
            os.remove(os.path.join(tgt, "up_1.rows.gz"))   # dangling link: present, unreadable
            third = summarize.current_summary(d)["lives"]["up_1.json"]["EXP2-T2"]["status"]
            self.assertEqual(third, "UNVERIFIED")
        finally:
            shutil.rmtree(d)
            shutil.rmtree(tgt)

    def test_f29_no_memo_between_input_and_verdict(self):
        import functools  # noqa: F401
        for mod in ("summarize", "observe", "evidence", "verdicts"):
            src = open(os.path.join(LIC, mod + ".py")).read()
            for marker in ("lru_cache", "functools.cache", "@cache"):
                self.assertNotIn(marker, src, (mod, marker))


    def test_f29_class_a_stale_last_response_is_not_recorded_as_this_cells_message(self):
        """Same class as F29 (state between input and verdict keyed on something weaker than the content): matrix.verdict reads the
        thread's last response; a status match alone must not attach another response's bytes to this cell."""
        import lib
        import matrix
        body = {"error": {"type": "security_exception", "reason": "current license is non-compliant for [rrf]"}, "status": 403}
        other = b'{"error":{"type":"security_exception","reason":"action is unauthorized"},"status":403}'
        lib._LAST.v = (403, other)   # the previous request had the same status and a different body
        spec = matrix.CELLS[("after", "retriever_rrf")] if ("after", "retriever_rrf") in matrix.CELLS else None
        phase = "after" if spec else [p for p, n in matrix.CELLS if n == "retriever_rrf"][0]
        est = {p: True for p in matrix.CELLS[(phase, "retriever_rrf")].prereqs}
        out = matrix.verdict(phase, "retriever_rrf", est, 403, body, None)
        self.assertNotIn("adapter_message", out, "another response's bytes were recorded as this cell's adapter message")
        lib._LAST.v = (403, json.dumps(body).encode())
        out = matrix.verdict(phase, "retriever_rrf", est, 403, body, None)
        self.assertIn("adapter_message", out)


# ---- F30: only the current-report writer may write summary_current.json --------------------------------------------------------
class LegacyWriterTests(unittest.TestCase):
    def test_f30_legacy_tools_refuse_the_current_report_destination(self):
        import subprocess
        d = tempfile.mkdtemp()
        try:
            for f in os.listdir(RES):
                if f.startswith("up_1."):
                    shutil.copy(os.path.join(RES, f), d)
            out = os.path.join(d, "summary_current.json")
            for cmd in ([sys.executable, os.path.join(LIC, "summarize.py"), d, out],
                        [sys.executable, os.path.join(LIC, "recompute.py"), d, out]):
                p = subprocess.run(cmd, cwd=LIC, capture_output=True, text=True, timeout=600)
                self.assertNotEqual(p.returncode, 0, (cmd[1], "legacy tool wrote the current-report destination"))
                self.assertFalse(os.path.exists(out), cmd[1])
                self.assertIn("summary_current.json", p.stderr + p.stdout, cmd[1])
        finally:
            shutil.rmtree(d)

    def test_f30_readme_states_the_legacy_failure_behaviour_and_design_withdraws_the_blanket_claim(self):
        readme = open(os.path.join(RES, "README.md")).read()
        self.assertIn("Legacy tools", readme)
        self.assertIn("refuse", readme)
        design = open(os.path.join(LIC, "DESIGN-VERDICTS.md")).read()
        self.assertNotIn("The class is closed as follows", design)
        self.assertNotIn("writes no status of record", design)


# ---- review 13 F31 and F32: raw rows in, verdict out (the recorder and the reducer are part of what is tested) ---------------------
def committed_raw(name):
    """A committed life exactly as the loader parses it (stored raw-sourced figures still in place) and its raw rows."""
    import gzip
    R = summarize.parse_life(os.path.join(RES, name + ".json"), name.split("_")[0])
    with gzip.open(os.path.join(RES, name + ".rows.gz"), "rt") as fh:
        rows = [json.loads(l) for l in fh]
    return R, rows


def insert_row(rows, row):
    """Insert a workload row keeping completion (t1) order, as the recorder writes them."""
    out = list(rows)
    i = next((k for k, r in enumerate(out) if r[1] > row[1]), len(out))
    out.insert(i, row)
    return out


def through_loader(R, rows, gate, poll=None, oracle=None):
    """The real path: typed parse of the raw rows, reduction (summarize.rebuild), then the verdict."""
    R = copy.deepcopy(R)
    summarize.rebuild(R, {"rows": rows, "poll": poll, "oracle": oracle})
    return verdicts.judge(gate, R)


class RecorderLevelReview13Tests(unittest.TestCase):
    def test_f31_a_control_failure_in_the_first_second_after_trial_activation_is_not_a_pass(self):
        R, rows = committed_raw("up_1")
        self.assertEqual(through_loader(R, rows, "EXP2-T5-up").status, "PASS", "the unmodified committed rows must pass (non-vacuous)")
        t = R["start_trial"]["t_resp"]
        bad = insert_row(rows, [t + 0.1, t + 0.15, "ctl", 0, 0, None, None,
                                'BackendError: HTTP 503: {"error":{"type":"unavailable_shards_exception"},"status":503}'])
        v = through_loader(R, bad, "EXP2-T5-up")
        self.assertNotEqual(v.status, "PASS", "a control request failing 0.1 s after the start_trial response must not pass: %s" % v.reason)

    def _verify(self, sem_rows, acked, present, sem_present, t_req, t_resp, direction):
        import inspect
        import exp2
        w = object.__new__(exp2.Workload)
        w.acked, w.attempted_failed, w.sem_rows = dict(acked), [], [list(r) for r in sem_rows]
        pages = [[{"_id": i} for i in present], []]

        def fake(method, path, body=None, **kw):
            if "lic-sem2/_search" in path:
                return 200, {"hits": {"total": {"value": len(sem_present)}, "hits": [{"_id": i} for i in sem_present]},
                             "_shards": {"failed": 0}}, 1
            if "_search" in path and method == "POST" and ("scroll" in path):
                return 200, {"_scroll_id": "x", "hits": {"hits": pages.pop(0) if pages else []}, "_shards": {"failed": 0}}, 1
            return 200, {"_shards": {"failed": 0}}, 1
        old = exp2.req
        exp2.req = fake
        try:
            if "t_req" in inspect.signature(w.verify_writes).parameters:
                return w.verify_writes(t_req=t_req, t_resp=t_resp, direction=direction)
            return w.verify_writes()   # the reviewed head records without transition times
        finally:
            exp2.req = old

    def _t4(self, sem_rows, sem_present):
        R = _healthy_for_t4()
        t_req, t_resp = 101.9, 102.0
        R["start_basic"].update(t_req=t_req, t_resp=t_resp)
        R["exp2"]["writes"] = self._verify(sem_rows, {"w0-000000": 101.0, "w0-000001": 103.0}, ["w0-000000", "w0-000001"], sem_present,
                                           t_req, t_resp, "down")
        return verdicts.judge("EXP2-T4", R)

    def test_f32_a_semantic_write_acknowledged_and_present_on_basic_is_not_a_pass(self):
        lic = {"type": "security_exception", "reason": "current license is non-compliant for [inference]"}
        ok = self._t4([[100.0, "s000000", 201, None], [102.1, "s000001", 403, lic]], ["s000000"])
        self.assertEqual(ok.status, "PASS", "a loud Basic failure, nothing acknowledged or present on Basic, must pass: %s" % ok.reason)
        v = self._t4([[100.0, "s000000", 201, None], [102.1, "s000001", 201, None]], ["s000000", "s000001"])
        self.assertNotEqual(v.status, "PASS", "a semantic write acknowledged at 102.1 s, after the Basic response at 102.0 s, and present "
                                              "must not pass: %s" % v.reason)


# ---- review 13 audit: one raw-row violation per conjunct, through the real path ---------------------------------------------------
# Every conjunct of every gate (verdicts.GATES[...].conjuncts) has an entry: a builder that changes the RAW record of a passing base so
# that ONLY that conjunct stops being established, run through parse, reduction (summarize.rebuild) and the verdict on the gate view
# (summarize.judged). `also` names conjuncts the same raw change unavoidably moves too (stated in CLAUSE-AUDIT.md); `whole` marks a
# conjunct the evidence type enforces, whose violation leaves the evidence unparsed (every conjunct NOT_ESTABLISHED). Level "figure"
# marks the cases with no raw record: the EXP3 migration figures (never saved) and a stale post-transition window (the recorder
# writes the window start; the conjunct guards a figure computed elsewhere).
LIC_ERR = {"type": "security_exception", "reason": "current license is non-compliant for [inference]"}
MISSING_EP = {"type": "resource_not_found_exception", "reason": "Inference endpoint not found [lic-stub]"}
NOT_LIC = 'BackendError: HTTP 503: {"error":{"type":"unavailable_shards_exception","reason":"no shard"},"status":503}'
_BASES = {}


def _gz(name, kind):
    import gzip
    with gzip.open(os.path.join(RES, "%s.%s.gz" % (name, kind)), "rt") as fh:
        return [json.loads(l) for l in fh] if kind == "rows" else json.load(fh)


def _writes_base(R, mode):
    """A synthetic raw write record (the committed lives were recorded without one) that passes every EXP2-T4 conjunct."""
    from tests.test_evidence import _writes_record
    return _writes_record(R, mode)


def raw_base(mode):
    """(life as the loader parses it, raw files): committed down_7 (rows, poll, oracle) or up_1 (rows), each with a synthetic write
    record and the matrix-cell fields the review-12 recorder writes (clauses.augment_base, the adapter view of the latch cells)."""
    if mode not in _BASES:
        name = "down_7" if mode == "down" else "up_1"
        R = summarize.parse_life(os.path.join(RES, name + ".json"), mode)
        clauses.augment_base(R)
        if mode == "down":
            from tests.test_evidence import _with_adapter_view
            _with_adapter_view(R)
        raw = {"rows": _gz(name, "rows"), "poll": _gz(name, "poll") if mode == "down" else None,
               "oracle": _gz(name, "oracle") if mode == "down" else None, "writes": _writes_base(R, mode)}
        for p in raw["poll"] or ():   # the polled counterpart of augment_base: the review-12 fourth element, docs_in_payload
            d = p.get("dls_search")
            if d and len(d) == 3:
                d.append(d[2] if d[1] == 200 else 0)
        _BASES[mode] = (R, raw)
    R, raw = _BASES[mode]
    return copy.deepcopy(R), copy.deepcopy(raw)


def run_gate(gate, R, raw, level="raw"):
    """-> ({conjunct: state}, Verdict) through the real path (raw: rebuild then the gate view; figure: the verdict on the life)."""
    if level == "raw":
        R = copy.deepcopy(R)
        summarize.rebuild(R, raw)
        v, parts = summarize.judged(gate, R)
    else:
        v, parts = verdicts.judge_parts(gate, R)
    return {p.cid: p.state for p in parts}, v


def _t(R):
    tr = R["start_basic"] if R["mode"] == "down" else R["start_trial"]
    return tr["t_req"], tr["t_resp"]


def _rows_edit(raw, keep=None, edit=None):
    rows = raw["rows"]
    if keep is not None:
        rows = [r for r in rows if keep(r)]
    if edit is not None:
        rows = [edit(list(r)) for r in rows]
    raw["rows"] = rows


def _first_refusal_seq(raw, R, probe="rrf"):
    q, r = _t(R)
    return next(i for i, p in enumerate(raw["poll"]) if p["_t1"] >= r - 0.5 and p.get(probe, [None])[0] == "LICENCE_ERROR")


def _set_poll(raw, cond, probe, value):
    for p in raw["poll"]:
        if cond(p):
            p[probe] = list(value)


def v_refused_each(R, raw):
    q, r = _t(R)
    _set_poll(raw, lambda p: p["_t1"] >= r - 0.5, "rrf", ["OTHER_ERROR", 503])


def v_within_5s(R, raw):
    q, r = _t(R)
    _set_poll(raw, lambda p: r - 0.5 <= p["_t1"] < r + 5.5, "rrf", ["OTHER_ERROR", 503])


def v_no_success_after(R, raw):
    raw["poll"][_first_refusal_seq(raw, R) + 3]["rrf"] = ["AVAILABLE", 200]


def v_observed_after(R, raw):
    del raw["poll"][_first_refusal_seq(raw, R) + 3]["rrf"]


def v_answered_before(R, raw):   # no rrf answer before the request (a refusal there would also be a success after it)
    q, r = _t(R)
    _set_poll(raw, lambda p: p["_t1"] < q, "rrf", ["OTHER_ERROR", 503])


def v_examined(R, raw):   # keep fewer than 20,000 successful auto/auto+linear answers, both labels still present
    n = [0]

    def keep(x):
        if x[2] in ("auto", "lin") and x[4]:
            n[0] += 1
            return n[0] % 3 != 0
        return True
    _rows_edit(raw, keep)


def _edit_first(raw, cond, change):
    done = [False]

    def edit(x):
        if not done[0] and cond(x):
            change(x)
            done[0] = True
        return x
    _rows_edit(raw, edit=edit)


def v_no_mismatch(R, raw):
    q, r = _t(R)
    _edit_first(raw, lambda x: x[2] == "auto" and x[4] and x[5] == "client" and x[0] > r + 5, lambda x: x.__setitem__(6, ["d99999"]))


def v_both_labels(R, raw):
    _rows_edit(raw, lambda x: not (x[2] in ("auto", "lin") and x[4] and x[5] == "backend"))


def v_oracle(R, raw):
    qi = next(x[3] for x in raw["rows"] if x[2] == "lin" and x[4] and x[5] == "backend")
    del raw["oracle"]["linear"][str(qi)]


def v_control_readers(R, raw):
    _edit_first(raw, lambda x: x[2] == "ctl" and x[4] and len(set(x[6])) > 1, lambda x: x.__setitem__(6, list(reversed(x[6]))))


def v_control_floor(R, raw):   # the broken arm's client answers made legal: the check is not shown sensitive
    import exp2
    legs = {int(k): v for k, v in raw["oracle"]["legs"].items()}

    def edit(x):
        if x[2] == "brk" and x[4] and x[5] == "client":
            x[6] = exp2._client_ids(legs[x[3]])
        return x
    _rows_edit(raw, edit=edit)


def _insert(raw, rows):
    out = raw["rows"]
    for row in rows:
        out = insert_row(out, row)
    raw["rows"] = out


def v_t2_share(R, raw):   # isolated lexical failures 1.05 s apart: more than 0.1% of reads, no window, no auto run
    ev = R["exp2"]["events"]
    n = int(0.0011 * sum(1 for x in raw["rows"] if x[2] in ("auto", "lin", "lex", "vec"))) + 1
    start = ev["t_start"] + 0.5
    assert start + 1.05 * n < ev["t_stop"], "too short a life for isolated failures"
    _insert(raw, [[start + 1.05 * i, start + 1.05 * i + 0.01, "lex", 0, 0, None, None, NOT_LIC] for i in range(n)])


def v_t2_run(R, raw):   # a 2.5 s interval in which every auto request fails
    q, r = _t(R)
    a = r + 20.0
    _rows_edit(raw, lambda x: not (x[2] in ("auto", "lin") and x[4] and a - 0.5 <= x[0] <= a + 3.0))
    _insert(raw, [[a + 0.1 * i, a + 0.1 * i + 0.05, "auto", 0, 0, None, None, NOT_LIC] for i in range(26)])


def v_t2_window(R, raw):   # lexical failures 0.5 s apart over 3 s: a failure window, no auto run
    q, r = _t(R)
    a = r + 30.0
    _insert(raw, [[a + 0.5 * i, a + 0.5 * i + 0.01, "lex", 0, 0, None, None, NOT_LIC] for i in range(7)])


def v_t2_phases(R, raw):
    q, r = _t(R)
    _rows_edit(raw, lambda x: not (x[2] == "vec" and x[0] >= r))


def v_t3_flip(R, raw):
    q, r = _t(R)
    lim = r + (5.5 if R["mode"] == "down" else R["ttl"] + 5.5)
    lab = "client" if R["mode"] == "down" else "backend"
    _rows_edit(raw, lambda x: not (x[2] == "auto" and x[4] and x[5] == lab and x[1] < lim))


def v_t3_stays(R, raw):
    q, r = _t(R)
    lab = "backend" if R["mode"] == "down" else "client"
    _insert(raw, [[r + 40.0, r + 40.01, "auto", 0, 1, lab, ["d00001"], None]])


def v_t3_before(R, raw):
    q, r = _t(R)
    other = "client" if R["mode"] == "down" else "backend"
    _edit_first(raw, lambda x: x[2] == "auto" and x[4] and x[1] < q - 5, lambda x: x.__setitem__(5, other))


def _ctl_fail_share(raw, sel):
    """Turn every 8th selected control failure into a non-licence 503 (12.5% > 5%)."""
    n = [0]

    def edit(x):
        if x[2] == "ctl" and not x[4] and sel(x):
            n[0] += 1
            if n[0] % 8 == 0:
                x[7] = NOT_LIC
        return x
    _rows_edit(raw, edit=edit)


def v_t5_down(R, raw):
    q, r = _t(R)
    _ctl_fail_share(raw, lambda x: x[0] >= r + 1.0)


def v_t5_up_before(R, raw):
    q, r = _t(R)
    _ctl_fail_share(raw, lambda x: x[1] < q)


def v_t5_up_after(R, raw):   # review 13 F31: a failure 0.1 s after the start_trial response
    q, r = _t(R)
    _insert(raw, [[r + 0.1, r + 0.15, "ctl", 0, 0, None, None, NOT_LIC]])


def f_window(start):
    def v(R, raw):
        summarize.rebuild(R, raw)
        key = "T5_control_after_1s" if R["mode"] == "down" else "T5_control_after_trial"
        R["exp2"][key]["start_rel_resp_s"] = start
    return v


def _w(raw):
    return raw["writes"]


def v_t4_lost(R, raw):
    _w(raw)["live_present"].remove("w0-000001")


def v_t4_phases(R, raw):
    q, r = _t(R)
    _w(raw)["acked"]["w0-000001"] = q - 1


def v_t4_control(R, raw):
    _w(raw)["live_present"].append("__injected_lost_write__")


def _basic_rows(R, raw):
    q, r = _t(R)
    return [x for x in _w(raw)["sem_rows"] if (x[0] >= r if R["mode"] == "down" else x[4] < q)]


def v_t4_attempted(R, raw):
    b = {x[1] for x in _basic_rows(R, raw)}
    _w(raw)["sem_rows"] = [x for x in _w(raw)["sem_rows"] if x[1] not in b]


def v_t4_loud(R, raw):
    x = _basic_rows(R, raw)[-1]
    x[2], x[3] = 404, {"type": "index_not_found_exception", "reason": "no such index [lic-sem2]"}


def v_t4_acked(R, raw):   # the reviewer's F32 case: acknowledged on Basic and present
    x = _basic_rows(R, raw)[0]
    x[2], x[3] = 201, None
    _w(raw)["sem_present"].append(x[1])


def v_t4_present(R, raw):
    _w(raw)["sem_present"].append(_basic_rows(R, raw)[0][1])


def v_t4_classified(R, raw):   # a non-loud semantic failure outside the Basic interval
    q, r = _t(R)
    t = q - 3 if R["mode"] == "down" else r + 8
    _w(raw)["sem_rows"].append([t, "s000009", 503, {"type": "unavailable_shards_exception"}, t + 0.1])


def cell(path, value):
    def v(R, raw):
        _put(R, path, value)
    return v


def cells(*edits):
    def v(R, raw):
        for p, x in edits:
            _put(R, p, x)
    return v


_MIG = "exp3.migration."
Violation = collections.namedtuple("Violation", "gate mode fn also whole level doc")


def _v(gate, mode, fn, doc, also=(), whole=False, level="raw"):
    return Violation(gate, mode, fn, tuple(also), whole, level, doc)


VIOLATIONS = {
    "EXP1-T2.refused-each": _v("EXP1-T2", "down", v_refused_each, "poll: rrf never refused after the transition (every later rrf cell an HTTP 503)", whole=True),
    "EXP1-T2.within-5s": _v("EXP1-T2", "down", v_within_5s, "poll: rrf cells from t_resp-0.5 to t_resp+5.5 s made HTTP 503, so its first refusal is after 5.5 s"),
    "EXP1-T2.no-success-after": _v("EXP1-T2", "down", v_no_success_after, "poll: one rrf answer 200 three polls after its first refusal"),
    "EXP1-T2.observed-after": _v("EXP1-T2", "down", v_observed_after, "poll: the rrf key missing from one poll after its first refusal"),
    "EXP1-T2.answered-before": _v("EXP1-T2", "down", v_answered_before, "poll: rrf answered no poll before the start_basic request (every earlier rrf cell an HTTP 503)"),
    "EXP1-T3.message": _v("EXP1-T3", "down", cell("exp1_after.retriever_rrf.adapter_message", "HTTP 403: forbidden"),
                          "cell: a latch message on which the real detector says False while the record says True", whole=True),
    "EXP1-T3.detector": _v("EXP1-T3", "down", cells(("exp1_after.retriever_rrf.adapter_message", clauses.NOT_LICENCE_403),
                                                    ("exp1_after.retriever_rrf.adapter_detects", False)),
                           "cell: an authorisation 403 message the adapter does not detect as a licence error"),
    "EXP1-T4.control": _v("EXP1-T4", "down", cell("exp1_trial.dls_search_as_user.only_kw_a", False),
                          "cell: the trial control returned a forbidden document", whole=True),
    "EXP1-T4.refused": _v("EXP1-T4", "down", cells(("exp1_after.dls_search_as_user.status", 200), ("exp1_after.dls_search_as_user.class", "AVAILABLE")),
                          "cell: the DLS user's search answered 200 after start_basic"),
    "EXP1-T4.no-document": _v("EXP1-T4", "down", cell("exp1_after.dls_search_as_user.docs_in_payload", 1), "cell: a 403 whose body carries one document"),
    "EXP1-T4-battery.control": _v("EXP1-T4-battery", "down", cell("dls_battery_trial.get_hidden_doc1.returned_hidden_doc", True),
                                  "cell: the hidden document readable on trial", whole=True),
    "EXP1-T4-battery.refused": _v("EXP1-T4-battery", "down", cell("dls_battery_after.knn_search.status", 200), "cell: kNN read path 200 after start_basic"),
    "EXP1-T4-battery.no-document": _v("EXP1-T4-battery", "down", cell("dls_battery_after.esql.docs_in_payload", 1), "cell: an ES|QL refusal carrying one row"),
    "EXP2-T1.examined": _v("EXP2-T1", "down", v_examined, "rows: every third successful auto/auto+linear answer removed (fewer than 20,000 left)"),
    "EXP2-T1.no-mismatch": _v("EXP2-T1", "down", v_no_mismatch, "rows: one client-labelled auto answer replaced by an id no leg returns"),
    "EXP2-T1.both-labels": _v("EXP2-T1", "down", v_both_labels, "rows: every backend-labelled auto/auto+linear answer removed",
                              also=("EXP2-T1.examined",)),
    "EXP2-T1.oracle": _v("EXP2-T1", "down", v_oracle, "oracle: the linear reference of a query an auto+linear backend answer used removed"),
    "EXP2-T1.control-readers": _v("EXP2-T1", "down", v_control_readers, "rows: one control reader's answer reordered"),
    "EXP2-T1.control-floor": _v("EXP2-T1", "down", v_control_floor, "rows: the broken arm's client answers replaced by the legal client RRF"),
    "EXP2-T2.share": _v("EXP2-T2", "down", v_t2_share, "rows: isolated lexical failures 1.05 s apart, just over 0.1% of reads"),
    "EXP2-T2.run": _v("EXP2-T2", "down", v_t2_run, "rows: 2.5 s in which every auto request fails (auto successes there removed)", also=("EXP2-T2.window",)),
    "EXP2-T2.window": _v("EXP2-T2", "down", v_t2_window, "rows: seven lexical failures 0.5 s apart (3 s window), auto untouched"),
    "EXP2-T2.phases": _v("EXP2-T2", "down", v_t2_phases, "rows: no vector request after the start_basic response"),
    "EXP2-T3.down": _v("EXP2-T3-down", "down", v_t3_flip, "rows: client-labelled auto answers completed before t_resp+5.5 s removed"),
    "EXP2-T3.down-stays": _v("EXP2-T3-down", "down", v_t3_stays, "rows: one backend-labelled auto answer 40 s after the response"),
    "EXP2-T3.down-before": _v("EXP2-T3-down", "down", v_t3_before, "rows: one auto answer before the request labelled client"),
    "EXP2-T3.up": _v("EXP2-T3-up", "up", v_t3_flip, "rows: backend-labelled auto answers completed before t_resp+ttl+5.5 s removed"),
    "EXP2-T3.up-stays": _v("EXP2-T3-up", "up", v_t3_stays, "rows: one client-labelled auto answer 40 s after the response"),
    "EXP2-T3.up-before": _v("EXP2-T3-up", "up", v_t3_before, "rows: one auto answer before the request labelled backend"),
    "EXP2-T4.lost": _v("EXP2-T4", "down", v_t4_lost, "writes: an acknowledged plain write absent"),
    "EXP2-T4.phases": _v("EXP2-T4", "down", v_t4_phases, "writes: no plain write acknowledged after the response"),
    "EXP2-T4.control": _v("EXP2-T4", "down", v_t4_control, "writes: the injected id present, so the control cannot report it"),
    "EXP2-T4.basic-attempted": _v("EXP2-T4", "down", v_t4_attempted, "writes: no semantic write issued on Basic"),
    "EXP2-T4.basic-loud": _v("EXP2-T4", "down", v_t4_loud, "writes: a Basic semantic failure that is a 404 for a missing index", also=("EXP2-T4.classified",)),
    "EXP2-T4.basic-none-acked": _v("EXP2-T4", "down", v_t4_acked, "writes: a semantic write acknowledged on Basic and present (review 13 F32)",
                                   also=("EXP2-T4.basic-none-present",)),
    "EXP2-T4.basic-none-present": _v("EXP2-T4", "down", v_t4_present, "writes: a loud Basic failure whose document is present",
                                     also=("EXP2-T4.classified",)),
    "EXP2-T4.classified": _v("EXP2-T4", "down", v_t4_classified, "writes: a 503 semantic failure on trial (outside the Basic interval)"),
    "EXP2-T5.down": _v("EXP2-T5-down", "down", v_t5_down, "rows: 12.5% of control failures after 1 s turned into 503 without licence text"),
    "EXP2-T5.down-window": _v("EXP2-T5-down", "down", f_window(0.5), "figure: a post-transition count whose window starts 0.5 s after the response",
                              level="figure"),
    "EXP2-T5.up-before": _v("EXP2-T5-up", "up", v_t5_up_before, "rows: 12.5% of control failures before the request turned into 503"),
    "EXP2-T5.up-after": _v("EXP2-T5-up", "up", v_t5_up_after, "rows: a control 503 0.1 s after the start_trial response (review 13 F31)"),
    "EXP2-T5.up-window": _v("EXP2-T5-up", "up", f_window(1.0), "figure: a post-trial count whose window starts 1 s after the response", level="figure"),
    "EXP3-T2.counts": _v("EXP3-migration", "down", cell(_MIG + "bbq_disk_to_hnsw.dst_count", 1), "figure: destination count 1", level="figure"),
    "EXP3-T2.vectors": _v("EXP3-migration", "down", cell(_MIG + "bbq_disk_to_bbq_hnsw.vector_mismatches", 1), "figure: one vector mismatch", level="figure"),
    "EXP3-T2.knn": _v("EXP3-migration", "down", cell(_MIG + "bbq_disk_to_hnsw.knn_self_top1", 0), "figure: kNN self-hit 0", level="figure"),
    "EXP3-T2.ops": _v("EXP3-migration", "down", cell(_MIG + "migrated_basic_ops.lic3-sem-mig.update", 500), "figure: update on a migrated index 500",
                      level="figure"),
    "EXP3.ground-truth": _v("EXP3-migration", "down", cell(_MIG + "bbq_disk_to_hnsw.src_vs_ground_truth_max_abs", 0.5),
                            "figure: source vectors 0.5 from the ground truth", level="figure"),
    "EXP3-T3.plain": _v("EXP3-migration", "down", cell(_MIG + "control_plain_to_hnsw.vector_mismatches", 1), "figure: the plain copy changed", level="figure"),
    "EXP3-T3.corrupted": _v("EXP3-migration", "down", cell(_MIG + "control_corrupted_vs_true_source.vector_mismatches", 0),
                            "figure: the corrupted copy not caught", level="figure"),
    "EXP3-T4.control": _v("EXP3-semonly-control", "down", cell("semonly_trial.match.status", 500), "cell: match on the semantic-only index 500"),
}


# ---- each half of a compound conjunct (review 13, a conjunct whose predicate is an AND): one more raw or figure violation per half
def v_refused_before(R, raw):   # a refusal recorded in the first poll, well before the window
    raw["poll"][0]["rrf"] = ["LICENCE_ERROR", 403]


def v_both_labels_client(R, raw):
    _rows_edit(raw, lambda x: not (x[2] in ("auto", "lin") and x[4] and x[5] == "client"))


def v_ctl_without_oracle(R, raw):   # a control reader's query loses its rrf reference
    qi = next(x[3] for x in raw["rows"] if x[2] == "ctl" and x[4])
    del raw["oracle"]["rrf"][str(qi)]


def v_t3_before_none(R, raw):   # no auto answer completed before the request
    q, r = _t(R)
    _rows_edit(raw, lambda x: not (x[2] == "auto" and x[4] and x[1] < q))


def v_t4_sem_lost(R, raw):   # the semantic write acknowledged on trial is absent
    w = _w(raw)
    acked = [x[1] for x in w["sem_rows"] if 200 <= x[2] < 300]
    w["sem_present"] = [i for i in w["sem_present"] if i != acked[0]]


def v_t4_failed_present(R, raw):   # a loud semantic failure on trial whose document is present
    q, r = _t(R)
    t = q - 3 if R["mode"] == "down" else r + 8
    _w(raw)["sem_rows"].append([t, "s000009", 403, LIC_ERR, t + 0.1])
    _w(raw)["sem_present"].append("s000009")


def fig(*edits):   # figure-level: each (dotted path, function of the old value)
    def v(R, raw):
        for path, f in edits:
            d = R
            ks = path.split(".")
            for k in ks[:-1]:
                d = d[k]
            d[ks[-1]] = f(d[ks[-1]])
    return v


_M = _MIG
dec = lambda x: x - 1   # noqa: E731
HALVES = {
    "EXP1-T2.answered-before#refused-before": _v("EXP1-T2", "down", v_refused_before, "poll: rrf refused (403 licence) in the first poll, before the window"),
    "EXP2-T1.both-labels#client": _v("EXP2-T1", "down", v_both_labels_client, "rows: every client-labelled auto/auto+linear answer removed",
                                     also=("EXP2-T1.examined",)),
    "EXP2-T1.control-readers#without-oracle": _v("EXP2-T1", "down", v_ctl_without_oracle,
                                                 "oracle: the rrf reference of a query a control reader answered removed", also=("EXP2-T1.oracle",)),
    "EXP2-T3.down-before#none": _v("EXP2-T3-down", "down", v_t3_before_none, "rows: every auto answer completed before the request removed"),
    "EXP2-T3.up-before#none": _v("EXP2-T3-up", "up", v_t3_before_none, "rows: every auto answer completed before the request removed"),
    "EXP2-T4.lost#semantic": _v("EXP2-T4", "down", v_t4_sem_lost, "writes: the semantic write acknowledged on trial absent"),
    "EXP2-T4.classified#present": _v("EXP2-T4", "down", v_t4_failed_present, "writes: a loud semantic failure on trial whose document is present"),
    "EXP3-T2.counts#bbq-hnsw": _v("EXP3-migration", "down", fig((_M + "bbq_disk_to_bbq_hnsw.dst_count", dec)), "figure: bbq_hnsw copy one short", level="figure"),
    "EXP3-T2.counts#semantic": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.dst_count", dec)), "figure: semantic copy one short",
                                  level="figure"),
    "EXP3-T2.counts#source-hnsw": _v("EXP3-migration", "down", fig((_M + "bbq_disk_to_hnsw.src_count", dec), (_M + "bbq_disk_to_hnsw.dst_count", dec)),
                                     "figure: hnsw source and copy agree, one under the independent count", level="figure"),
    "EXP3-T2.counts#source-bbq-hnsw": _v("EXP3-migration", "down", fig((_M + "bbq_disk_to_bbq_hnsw.src_count", dec),
                                                                       (_M + "bbq_disk_to_bbq_hnsw.dst_count", dec)),
                                         "figure: bbq_hnsw source and copy agree, one under the independent count", level="figure"),
    "EXP3-T2.counts#source-semantic": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.docs", dec), (_M + "semantic_to_text_vector.dst_count", dec)),
                                         "figure: semantic docs and copy agree, one under the independent count", level="figure"),
    "EXP3-T2.counts#source-plain": _v("EXP3-migration", "down", fig((_M + "control_plain_to_hnsw.src_count", dec), (_M + "control_plain_to_hnsw.dst_count", dec)),
                                      "figure: plain source and copy agree, one under the independent count", level="figure"),
    "EXP3-T2.vectors#hnsw": _v("EXP3-migration", "down", fig((_M + "bbq_disk_to_hnsw.vector_mismatches", lambda x: 1)), "figure: one hnsw vector mismatch",
                               level="figure"),
    "EXP3-T2.vectors#semantic": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.vector_mismatches_vs_stored", lambda x: 1)),
                                   "figure: one semantic vector differs from the stored one", level="figure"),
    "EXP3-T2.vectors#abs-err": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.worst_abs_err_vs_stub", lambda x: 0.01)),
                                  "figure: worst absolute error 0.01", level="figure"),
    "EXP3-T2.vectors#cosine": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.worst_cosine_vs_stub", lambda x: 0.999)),
                                 "figure: worst cosine 0.999", level="figure"),
    "EXP3-T2.vectors#skipped": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.skipped_not_single_chunk", lambda x: 1),
                                                                (_M + "semantic_to_text_vector.docs", dec), (_M + "semantic_to_text_vector.dst_count", dec)),
                                  "figure: one multi-chunk document skipped (docs and copy one fewer)", level="figure"),
    "EXP3-T2.knn#bbq-hnsw": _v("EXP3-migration", "down", fig((_M + "bbq_disk_to_bbq_hnsw.knn_self_top1", lambda x: 0)), "figure: bbq_hnsw kNN self-hit 0",
                               level="figure"),
    "EXP3-T2.knn#semantic": _v("EXP3-migration", "down", fig((_M + "semantic_to_text_vector.knn_self_top1", lambda x: 0)), "figure: semantic kNN self-hit 0",
                               level="figure"),
    "EXP3-T3.plain#count": _v("EXP3-migration", "down", fig((_M + "control_plain_to_hnsw.dst_count", dec)), "figure: plain control copy one short",
                              level="figure"),
}
ALL_VIOLATIONS = sorted(list(VIOLATIONS.items()) + list(HALVES.items()))


def _base_mode(gate):
    s = verdicts.GATES[gate].scope
    return "down" if s == "both" else s


class ConjunctViolationTests(unittest.TestCase):
    def test_every_conjunct_quotes_its_registered_line_and_names_a_recorder(self):
        import audit
        with open(os.path.join(LIC, "THRESHOLDS.md")) as fh:
            lines = fh.read().split("\n")
        for cid, d in clauses.CONJUNCTS.items():
            self.assertIn(d.quote, lines[d.line - 1], cid)
            audit.def_line(d.recorder)   # raises KeyError when the recorder function does not exist
        gated = {cid for g in verdicts.GATES.values() for cid in g.conjuncts} | {c.cid for c in clauses.CLAUSES if c.kind == "LIMIT"}
        self.assertEqual(sorted(gated ^ set(clauses.CONJUNCTS)), [])
        for c in clauses.CLAUSES:
            self.assertTrue(set(c.conj) <= set(clauses.CONJUNCTS), c.cid)
            if c.gates:
                self.assertTrue(c.conj, (c.cid, "a gated clause without conjuncts"))
                own = {x for g in c.gates for x in verdicts.conjuncts_of(g)}
                self.assertTrue(set(c.conj) <= own, (c.cid, sorted(set(c.conj) - own), "not decided by its gates"))

    def test_a_conjunct_the_gate_function_does_not_judge_is_not_established(self):
        R, raw = raw_base("up")
        g = verdicts.GATES["EXP2-T5-up"]
        old = g.verdict
        try:
            verdicts.GATES["EXP2-T5-up"] = g._replace(verdict=lambda ev: [p for p in old(ev) if p.cid != "EXP2-T5.up-after"])
            states, v = run_gate("EXP2-T5-up", R, raw)
        finally:
            verdicts.GATES["EXP2-T5-up"] = g
        self.assertEqual(states["EXP2-T5.up-after"], "NOT_ESTABLISHED")
        self.assertEqual(v.status, "UNVERIFIED")

    def test_withheld_figures_fail_closed_without_the_loader_markers(self):
        from tests.test_evidence import _life
        R = _life("down_7")   # complete EXP2-T1 figures; then treated as stored (oracle source not rebuilt)
        self.assertEqual(summarize.judged("EXP2-T1", R)[0].status, "PASS")
        R["_rebuilt"] = [s for s in R["_rebuilt"] if s != "rows+oracle"]
        self.assertEqual(verdicts.judge("EXP2-T1", R).status, "PASS")   # the figures alone would pass
        self.assertEqual(summarize.judged("EXP2-T1", R)[0].status, "UNVERIFIED")
        for mark in ("absent", [], None):
            X = copy.deepcopy(R)
            if mark == "absent":
                del X["_stored_only"]
            else:
                X["_stored_only"] = mark
            self.assertNotEqual(summarize.judged("EXP2-T1", X)[0].status, "PASS", mark)
        X = copy.deepcopy(R)
        del X["_rebuilt"]   # never loaded: no figure is known to come from raw rows
        for g in ("EXP2-T1", "EXP2-T2", "EXP2-T3-down", "EXP2-T5-down"):
            self.assertNotEqual(summarize.judged(g, X)[0].status, "PASS", g)

    def test_committed_clause_audit_is_what_audit_py_writes(self):
        import audit
        with open(os.path.join(LIC, "CLAUSE-AUDIT.md")) as fh:
            self.assertEqual(fh.read(), audit.render(), "regenerate: cd licence && python3 audit.py CLAUSE-AUDIT.md")

    def test_every_conjunct_has_one_raw_violation_and_every_limit_is_never_established(self):
        for g in verdicts.GATES:
            for cid in verdicts.GATES[g].conjuncts:
                self.assertIn(cid, VIOLATIONS, (g, cid, "a conjunct no raw-row test violates"))
                self.assertEqual(VIOLATIONS[cid].gate, g, cid)
            for c in clauses.limits_for(g):
                R, raw = raw_base(_base_mode(g))
                states, _ = run_gate(g, R, raw, "figure" if g == "EXP3-migration" else "raw")
                self.assertEqual(states[c.cid], "NOT_ESTABLISHED", (g, c.cid))
        self.assertEqual(sorted(set(VIOLATIONS) ^ (set(clauses.CONJUNCTS) - {c.cid for c in clauses.CLAUSES if c.kind == "LIMIT"})), [])

    def test_bases_establish_every_conjunct(self):
        for g, gate in verdicts.GATES.items():
            for mode in (("down", "up") if gate.scope == "both" else (gate.scope,)):
                if g == "EXP2-T1" and mode == "up":
                    continue   # the up lives carry no saved oracle
                R, raw = raw_base(mode)
                states, v = run_gate(g, R, raw, "figure" if g == "EXP3-migration" else "raw")
                lim = {c.cid for c in clauses.limits_for(g)}
                self.assertEqual({c: s for c, s in states.items() if c not in lim and s != "ESTABLISHED"}, {}, (g, mode, v.reason))

    def test_violating_one_conjunct_in_the_raw_record_leaves_the_gate_and_its_clauses_off_pass(self):
        for key, x in ALL_VIOLATIONS:
            cid = key.split("#")[0]
            with self.subTest(conjunct=key):
                R, raw = raw_base(x.mode)
                x.fn(R, raw)
                states, v = run_gate(x.gate, R, raw, x.level)
                self.assertNotEqual(states[cid], "ESTABLISHED", (cid, v.reason))
                self.assertNotEqual(v.status, "PASS", cid)
                lim = {c.cid for c in clauses.limits_for(x.gate)}
                if not x.whole:
                    moved = sorted(c for c, s in states.items() if s != "ESTABLISHED" and c != cid and c not in lim and c not in x.also)
                    self.assertEqual(moved, [], (cid, "the raw change moved other conjuncts", v.reason))
                st = clauses.clause_statuses({x.gate: [("life", {c: (s, "") for c, s in states.items()})]})
                for c in clauses.CLAUSES:
                    if cid in c.conj:
                        self.assertNotEqual(st[c.cid]["status"], "PASS", (cid, c.cid))


def _healthy_for_t4():
    R = _healthy("down")
    R["exp2"].setdefault("events", {})
    return R


if __name__ == "__main__":
    unittest.main()
