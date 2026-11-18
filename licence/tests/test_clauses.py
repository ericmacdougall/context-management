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
                        ev, why = E.parse(verdicts.GATES[g].evidence, R)
                        if bad in (-1,) and isinstance(_get(base(g), w), bool):
                            continue
                        self.assertIsNone(ev, (c.cid, g, w, bad, "the gate still parses without its witness"))
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
        ch = idx.get("status_changes_review_12")
        self.assertIsInstance(ch, dict, "SUPERSEDED.json does not list the review-12 status changes")
        self.assertEqual(ch["count"], len(ch["changes"]))
        cur = json.load(open(os.path.join(RES, "summary_current.json")))
        for e in ch["changes"]:
            where = cur["aggregate"] if e["life"] == "aggregate" else cur["lives"][e["life"]]
            self.assertEqual(where[e["gate"]]["status"], e["to"], e)
        readme = open(os.path.join(RES, "README.md")).read()
        self.assertIn("%d statuses changed" % ch["count"], readme)


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


if __name__ == "__main__":
    unittest.main()
