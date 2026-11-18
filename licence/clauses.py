"""The clause table (gate review 12): THRESHOLDS.md clause by clause, starting from the REGISTERED RULES, not from the leaves the verdicts
read. One row per atomic clause:

  cid        stable id
  line       THRESHOLDS.md line (1-based); `quote` must be a substring of it (tests.test_clauses checks both)
  kind       PASS        a condition the gate must witness before it may PASS
             FAIL        a stated failure condition; `mutations` make it true and must move the gate to `expect`
             CONTROL     a control or witness the gate requires (missing or wrong: UNVERIFIED)
             SUBSTITUTE  the gate witnesses the clause through a measure that implies it (stated in `note`)
             LIMIT       registered wording no recorded field witnesses: forces the gate's PASS to UNVERIFIED (verdicts.judge)
             REPORTED    no gate: reported only; its status in summary_current.json is UNVERIFIED, never PASS
             RULE        a procedure or recording rule; not decided by a gate (UNVERIFIED in the report)
  gates      verdicts.GATES names that decide it
  witness    life paths (dotted) whose removal or corruption must make the gate UNVERIFIED (PASS, CONTROL, SUBSTITUTE)
  mutations  Mutation(gate, edits, expect): set each dotted path to its value (ABSENT deletes it) on a passing base life
  recorder   "file.py:function" that produces the witness
  verdict    "verdicts.py:function" that reads it
  note       what the row decides and why

tests.test_clauses generates the tests from this table and fails when a THRESHOLDS.md rule line is not cited, a gate is not cited, a
FAIL clause has no mutation, or a witness is not one the gate reads. summarize.current_summary reports every row ("clauses").
"""
import collections

ABSENT = object()
KINDS = ("PASS", "FAIL", "CONTROL", "SUBSTITUTE", "LIMIT", "REPORTED", "RULE")
Clause = collections.namedtuple("Clause", "cid line quote kind gates witness mutations recorder verdict note")
Mutation = collections.namedtuple("Mutation", "gate edits expect")

T2_PROBES = ("rrf", "linear", "semantic_query", "inference_use", "dls_search")
BATTERY = ("get_allowed_doc0", "get_hidden_doc1", "search_match_all", "search_term_hidden", "count", "mget", "msearch", "scroll_open",
           "knn_search", "termvectors_hidden", "wildcard_index", "esql")
LATCH_CELLS = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
NOT_LICENCE_403 = 'HTTP 403: {"error":{"type":"security_exception","reason":"action [indices:data/read/search] is unauthorized"}}'


def C(cid, line, quote, kind, gates=(), witness=(), mutations=(), recorder="", verdict="", note=""):
    return Clause(cid, line, quote, kind, tuple(gates), tuple(witness), tuple(mutations), recorder, verdict, note)


def M(gate, expect="FAIL", **edits):
    """Mutation: keyword names use "__" for "." (paths with "-" are passed through m())."""
    return Mutation(gate, tuple((k.replace("__", "."), v) for k, v in edits.items()), expect)


def m(gate, edits, expect="FAIL"):
    return Mutation(gate, tuple(edits), expect)


_TWIN = "exp3.migration."
CLAUSES = (
    # ---- Lives ---------------------------------------------------------------------------------------------------------------------
    C("LIVES-A", 12, "Life A (down): fresh Basic, start_trial", "RULE", note="procedure of a down life; recorded by life.py:down, not gated"),
    C("LIVES-B", 13, "Life B (up): fresh Basic, load running on Basic", "RULE", note="procedure of an up life; recorded by life.py:up"),
    C("LIVES-N", 14, "N = 5 lives of A and 5 of B", "RULE",
      note="inventory: summarize.CAMPAIGN (5 + 5 plus supplemental down_6, down_7); a missing life is UNVERIFIED for every gate. One B "
           "life (up_5) has ttl 300 s"),
    C("LIVES-WORST", 15, "worst case over lives and as a Wilson lower bound", "REPORTED",
      note="aggregate is FAIL if any life fails (summarize.current_summary); the Wilson bound is reported (summarize.aggregate), not gated"),
    # ---- EXP1 --------------------------------------------------------------------------------------------------------------------
    C("EXP1-T1", 23, "EXP1-T1 (determinism)", "REPORTED",
      note="no gate. Disclosed limit: a 400 with \"non-compliant\" is not counted as a licence refusal, only a 403 (summarize.LIMITS)"),
    C("EXP1-T2.bound", 24, "the first licence refusal arrives no later than 5 s after the start_basic response", "FAIL", ["EXP1-T2"],
      mutations=[M("EXP1-T2", **{"exp1_moment__%s__first_refusal__rel_resp_s" % p: 5.5, "exp1_moment__%s__first_refusal_rel_resp_s" % p: 5.5})
                 for p in T2_PROBES],
      recorder="observe.py:moments", verdict="verdicts.py:moment"),
    C("EXP1-T2.no-success", 24, "no probe returns success after its first refusal", "FAIL", ["EXP1-T2"],
      mutations=[M("EXP1-T2", **{"exp1_moment__%s__success_after_first_refusal" % p: 1}) for p in T2_PROBES],
      recorder="observe.py:moments", verdict="verdicts.py:moment"),
    C("EXP1-T2.refused", 24, "licensed probes (rrf, linear, semantic query, inference use, DLS search) are polled every 50 ms", "PASS",
      ["EXP1-T2"], witness=["exp1_moment.%s.first_refusal" % p for p in T2_PROBES] + ["exp1_moment.%s.n" % p for p in T2_PROBES],
      recorder="matrix.py:quick_probes", verdict="verdicts.py:moment",
      note="every mandatory probe must have its first refusal of its required Outcome case; a never-refused or skipped probe does not parse"),
    C("EXP1-T3.detector", 25, "A false is a finding (latch would not flip)", "FAIL", ["EXP1-T3"],
      mutations=[m("EXP1-T3", [("exp1_after.%s.adapter_message" % c, NOT_LICENCE_403), ("exp1_after.%s.adapter_detects" % c, False)])
                 for c in LATCH_CELLS],
      recorder="matrix.py:verdict", verdict="verdicts.py:latch"),
    C("EXP1-T3.message", 25, "the adapter's `_is_licence_error` must be true for the error text the adapter would build", "PASS", ["EXP1-T3"],
      witness=["exp1_after.%s.adapter_message" % c for c in LATCH_CELLS] + ["exp1_after.%s.adapter_detects" % c for c in LATCH_CELLS],
      recorder="matrix.py:verdict", verdict="verdicts.py:latch",
      note="the committed lives recorded only an error summary, not the message: UNVERIFIED for all of them"),
    C("EXP1-T3.every-cell", 25, "for every LICENCE_ERROR cell of a search path", "LIMIT", ["EXP1-T3"],
      recorder="matrix.py:verdict", verdict="verdicts.py:latch",
      note="only three fixed latch cells are witnessed, not every LICENCE_ERROR cell of a search path"),
    C("EXP1-T4.error", 26, "after start_basic a search as the DLS-restricted user must return an error", "PASS", ["EXP1-T4"],
      witness=["exp1_after.dls_search_as_user.status", "exp1_after.dls_search_as_user.class"],
      recorder="matrix.py:dls_search", verdict="verdicts.py:dls",
      note="an authorisation or licence refusal; any other error says nothing (UNVERIFIED), a success is a FAIL"),
    C("EXP1-T4.error-battery", 26, "after start_basic a search as the DLS-restricted user must return an error", "PASS", ["EXP1-T4-battery"],
      witness=["dls_battery_after.%s.status" % p for p in BATTERY], recorder="matrix.py:dls_battery", verdict="verdicts.py:battery",
      note="every read path of the battery: get, search, count, mget, msearch, scroll, kNN, termvectors, wildcard index, ES|QL"),
    C("EXP1-T4.no-document", 26, "returning any document is a FAIL (isolation)", "PASS", ["EXP1-T4"],
      witness=["exp1_after.dls_search_as_user.docs_in_payload", "exp1_moment.dls_search.docs_after_transition"],
      recorder="matrix.py:docs_in_payload", verdict="verdicts.py:dls",
      note="review 12 F28: the witness is the document count read from the body's shape whatever the HTTP status. The committed lives were "
           "recorded without it (their after cells keep class, status and an error summary): UNVERIFIED"),
    C("EXP1-T4.no-document-battery", 26, "returning any document is a FAIL (isolation)", "PASS", ["EXP1-T4-battery"],
      witness=["dls_battery_after.%s.docs_in_payload" % p for p in BATTERY], recorder="matrix.py:docs_in_payload", verdict="verdicts.py:battery",
      note="review 12 F28: returned_any_source was recorded only for 2xx answers and returned_hidden_doc covers only the hidden document, so "
           "the committed battery has no witness that a refused read path carried no document: UNVERIFIED"),
    C("EXP1-T4.document-fails", 26, "returning any document is a FAIL (isolation)", "FAIL", ["EXP1-T4", "EXP1-T4-battery"],
      mutations=[M("EXP1-T4", exp1_after__dls_search_as_user__docs_in_payload=1),
                 M("EXP1-T4", exp1_after__dls_search_as_user__returned=8),
                 M("EXP1-T4", exp1_moment__dls_search__docs_after_transition=1),
                 M("EXP1-T4", exp1_after__dls_search_as_user__status=200, exp1_after__dls_search_as_user__class="AVAILABLE")]
      + [M("EXP1-T4-battery", **{"dls_battery_after__%s__docs_in_payload" % p: 1}) for p in BATTERY]
      + [M("EXP1-T4-battery", **{"dls_battery_after__%s__returned_hidden_doc" % p: True}) for p in BATTERY]
      + [M("EXP1-T4-battery", **{"dls_battery_after__%s__status" % p: 200}) for p in BATTERY],
      recorder="matrix.py:dls_battery", verdict="verdicts.py:battery"),
    C("EXP1-T4.control", 26, "Control: the same search before the downgrade returns only the allowed documents", "CONTROL",
      ["EXP1-T4", "EXP1-T4-battery"],
      witness=["exp1_trial.dls_search_as_user.only_kw_a", "exp1_trial.dls_search_as_user.returned", "exp1_trial.dls_search_as_user.status"],
      recorder="matrix.py:dls_search", verdict="verdicts.py:dls", note="witness: allowed count 1..7 of 8, all of kw a"),
    C("EXP1-T4.control-battery", 26, "(witness: allowed count > 0 and < total)", "CONTROL", ["EXP1-T4-battery"],
      witness=["dls_battery_trial.get_allowed_doc0.returned_any_source", "dls_battery_trial.search_match_all.returned_any_source",
               "dls_battery_trial.get_hidden_doc1.status"],
      recorder="matrix.py:dls_battery", verdict="verdicts.py:battery", note="on trial the allowed document is readable and the hidden one is not"),
    # ---- EXP2 --------------------------------------------------------------------------------------------------------------------
    C("EXP2-T1.mismatch", 30, "Bound: 0 mismatches over all responses", "FAIL", ["EXP2-T1"],
      mutations=[M("EXP2-T1", exp2__T1_mismatches=1)], recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("EXP2-T1.examined", 30, "responses examined >= 20,000 per life", "FAIL", ["EXP2-T1"],
      mutations=[M("EXP2-T1", exp2__T1_examined=19999)], recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("EXP2-T1.compared", 30, "for every response labelled client the ids equal the independently computed client RRF", "PASS", ["EXP2-T1"],
      witness=["exp2.T1_examined", "exp2.T1_mismatches"], recorder="observe.py:correctness", verdict="verdicts.py:correctness",
      note="rebuilt from rows.gz + oracle.gz for down_7; stored (labelled 'from stored figure') for lives without saved oracle legs"),
    C("EXP2-T2.share", 31, "failed auto/lexical/vector read requests (anything not a result) <= 0.1% of requests", "FAIL", ["EXP2-T2"],
      mutations=[M("EXP2-T2", exp2__T2_failed_requests=1000, exp2__T2_failure_windows=[[100.0, 100.5, 1000]],
                   exp2__T2_longest_failure_window_s=0.5, exp2__T2_longest_consecutive_auto_failure_s=0.5)],
      recorder="observe.py:availability", verdict="verdicts.py:availability"),
    C("EXP2-T2.run", 31, "the longest run of consecutive failed auto requests (by time, any thread) <= 2.0 s", "FAIL", ["EXP2-T2"],
      mutations=[M("EXP2-T2", exp2__T2_longest_consecutive_auto_failure_s=3.0),
                 M("EXP2-T2", exp2__T2_failed_requests=1, exp2__T2_failure_windows=[[100.0, 103.0, 1]], exp2__T2_longest_failure_window_s=3.0)],
      recorder="observe.py:consecutive_auto_failure", verdict="verdicts.py:availability",
      note="review 12 closes the review-10 limit: the registered run (auto and auto+linear readers) is derived from rows.gz and gated; the "
           "joined window of all read arms is kept as an additional check"),
    C("EXP2-T2.witness", 31, "EXP2-T2 no outage", "PASS", ["EXP2-T2"],
      witness=["exp2.T2_failed_requests", "exp2.T2_non_control_requests", "exp2.T2_longest_consecutive_auto_failure_s"],
      recorder="observe.py:availability", verdict="verdicts.py:availability"),
    C("EXP2-T3.down", 32, "first auto response labelled client no later than 5.0 s after the start_basic response", "FAIL", ["EXP2-T3-down"],
      mutations=[M("EXP2-T3-down", exp2__T3_flip_latency_s=5.5)], recorder="observe.py:flip", verdict="verdicts.py:flip_down"),
    C("EXP2-T3.up", 32, "first auto response labelled backend no later than probe_ttl_s + 5.0 s after the start_trial response", "FAIL",
      ["EXP2-T3-up"], mutations=[M("EXP2-T3-up", exp2__T3_flip_latency_s=1000.0)], recorder="observe.py:flip", verdict="verdicts.py:flip_up"),
    C("EXP2-T3.witness", 32, "EXP2-T3 latch down", "PASS", ["EXP2-T3-down", "EXP2-T3-up"], witness=["exp2.T3_flip_latency_s"],
      recorder="observe.py:flip", verdict="verdicts.py:flip_down"),
    C("EXP2-T4.lost", 33, "acknowledged-but-absent = 0", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", exp2__writes__acked_absent=1)], recorder="exp2.py:verify_writes", verdict="verdicts.py:writes"),
    C("EXP2-T4.loud", 33, "every failure carries a 403 licence error or 404 for a missing endpoint, none acknowledged, none present", "FAIL",
      ["EXP2-T4"],
      mutations=[M("EXP2-T4", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=0, exp2__writes__sem_failed_other=1),
                 M("EXP2-T4", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=1, exp2__writes__sem_failed_other=0,
                   exp2__writes__sem_failed_present=1),
                 M("EXP2-T4", exp2__writes__sem_acked_absent=1)],
      recorder="exp2.py:classify_sem_failure", verdict="verdicts.py:writes"),
    C("EXP2-T4.witness", 33, "every acknowledged write id is present after a final refresh", "PASS", ["EXP2-T4"],
      witness=["exp2.writes.acked", "exp2.writes.acked_absent", "exp2.writes.sem_failed"], recorder="exp2.py:verify_writes",
      verdict="verdicts.py:writes"),
    C("EXP2-T4.reported", 33, "Unacknowledged-but-present and timeouts are counted separately and reported", "REPORTED"),
    C("EXP2-T5.down", 34, "a control below 95% invalidates the run", "FAIL", ["EXP2-T5-down"],
      mutations=[M("EXP2-T5-down", exp2__T5_control_after_1s__licence_refusals=0)], recorder="observe.py:control",
      verdict="verdicts.py:licence_down"),
    C("EXP2-T5.witness", 34, "EXP2-T5 control: arm strategy=backend after start_basic", "PASS", ["EXP2-T5-down"],
      witness=["exp2.T5_control_after_1s.n", "exp2.T5_control_after_1s.licence_refusals"], recorder="observe.py:control",
      verdict="verdicts.py:licence_down"),
    C("EXP2-T6", 35, "EXP2-T6 recorded, not gated", "REPORTED"),
    # ---- EXP3 --------------------------------------------------------------------------------------------------------------------
    C("EXP3-T1", 40, "EXP3-T1 every operation either succeeds with the same observable result as on trial", "REPORTED",
      note="no gate (summarize.exp3_checks lists silent differences)"),
    C("EXP3-T2.counts", 41, "doc counts equal source (300/300 and 200/200)", "SUBSTITUTE", ["EXP3-migration"],
      witness=[_TWIN + "bbq_disk_to_hnsw.dst_count", _TWIN + "bbq_disk_to_hnsw.src_count", "exp3.after_restart.counts_before"],
      mutations=(), recorder="exp3.py:migrate_vectors", verdict="verdicts.py:migration",
      note="equality with the independent source count (299/299 and 199/199 after an earlier delete), the registered intent"),
    C("EXP3-T2.count-fails", 41, "doc counts equal source", "FAIL", ["EXP3-migration"],
      mutations=[M("EXP3-migration", exp3__migration__bbq_disk_to_hnsw__dst_count=1),
                 M("EXP3-migration", exp3__migration__semantic_to_text_vector__dst_count=1)],
      recorder="exp3.py:migrate_vectors", verdict="verdicts.py:migration"),
    C("EXP3-T2.vectors", 41, "vectors equal element-wise to the stored source vectors", "FAIL", ["EXP3-migration"],
      mutations=[M("EXP3-migration", exp3__migration__bbq_disk_to_hnsw__vector_mismatches=1),
                 M("EXP3-migration", exp3__migration__bbq_disk_to_bbq_hnsw__vector_mismatches=1),
                 M("EXP3-migration", exp3__migration__semantic_to_text_vector__vector_mismatches_vs_stored=1)],
      recorder="exp3.py:check_vectors", verdict="verdicts.py:migration"),
    C("EXP3-T2.relative", 41, "within 1% relative of the stub's float vectors", "LIMIT", ["EXP3-migration"],
      recorder="exp3.py:check_semantic", verdict="verdicts.py:migration",
      note="not witnessed: absolute error 0.005 and cosine 0.9999 were recorded, the per-value 1% relative error was not (vectors not kept)"),
    C("EXP3-T2.knn", 41, "kNN self-hit top-1 >= 99% on the migrated index", "FAIL", ["EXP3-migration"],
      mutations=[M("EXP3-migration", exp3__migration__bbq_disk_to_hnsw__knn_self_top1=0)], recorder="exp3.py:check_vectors",
      verdict="verdicts.py:migration"),
    C("EXP3-T2.ops", 41, "the migrated index passes every Basic-available operation", "FAIL", ["EXP3-migration"],
      mutations=[m("EXP3-migration", [("exp3.migration.migrated_basic_ops.lic3-bbq_disk-mig.update", 403)]),
                 m("EXP3-migration", [("exp3.migration.migrated_basic_ops.lic3-sem-mig.force_merge", 500)])],
      recorder="exp3.py:migrate_vectors", verdict="verdicts.py:migration"),
    C("EXP3-T2.witness", 41, "EXP3-T2 migration recipe to a Basic-safe mapping", "PASS", ["EXP3-migration"],
      witness=[_TWIN + "bbq_disk_to_hnsw.vector_mismatches", _TWIN + "bbq_disk_to_hnsw.knn_self_top1", _TWIN + "semantic_to_text_vector",
               _TWIN + "migrated_basic_ops"], recorder="exp3.py:check_vectors", verdict="verdicts.py:migration"),
    C("EXP3-T3.control", 42, "a deliberately corrupted migration (one vector perturbed) must be caught", "FAIL", ["EXP3-migration"],
      mutations=[M("EXP3-migration", exp3__migration__control_corrupted_vs_true_source__vector_mismatches=0),
                 M("EXP3-migration", exp3__migration__control_plain_to_hnsw__vector_mismatches=1)],
      recorder="exp3.py:migrate_vectors", verdict="verdicts.py:migration"),
    C("EXP3-T3.witness", 42, "the same recipe applied to the plain index changes nothing", "CONTROL", ["EXP3-migration"],
      witness=[_TWIN + "control_plain_to_hnsw", _TWIN + "control_corrupted_vs_true_source"], recorder="exp3.py:migrate_vectors",
      verdict="verdicts.py:migration"),
    # ---- EXP4 --------------------------------------------------------------------------------------------------------------------
    C("EXP4-T1", 46, "EXP4-T1 safe in every state", "REPORTED", note="no gate"),
    C("EXP4-T2", 47, "EXP4-T2 re-probe", "REPORTED", note="no gate"),
    C("EVIDENCE-RULES", 50, "Every result file carries: code commit, UTC time, parameters", "RULE", note="recording rule; not gated"),
    # ---- Addendum A --------------------------------------------------------------------------------------------------------------
    C("A.http-patch", 53, "Harness deviation, recorded in every result", "REPORTED"),
    C("A.T1-control", 54, "Floor: the check flags >= 90% of its client-labelled responses", "FAIL", ["EXP2-T1"],
      mutations=[M("EXP2-T1", exp2__T1_control_broken_fallback__flagged_wrong=0, exp2__T1_control_broken_fallback__flag_rate=0.0)],
      recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("A.T1-control-witness", 54, "EXP2-T1 control (sensitivity of the wrong-answer check)", "CONTROL", ["EXP2-T1"],
      witness=["exp2.T1_control_broken_fallback.client_labelled", "exp2.T1_control_broken_fallback.flagged_wrong"],
      recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("A.T4-control", 55, "the write verifier is self-tested by injecting one acknowledged-but-absent id", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", exp2__writes__control_injected_loss_detected=False)], recorder="exp2.py:verify_writes",
      verdict="verdicts.py:writes"),
    C("A.T4-control-witness", 55, "it must report it", "CONTROL", ["EXP2-T4"], witness=["exp2.writes.control_injected_loss_detected"],
      recorder="exp2.py:verify_writes", verdict="verdicts.py:writes"),
    C("A.threads", 56, "Workload threads: 8 auto, 2 auto+linear", "RULE"),
    C("A.ground-truth", 57, "the ground truth (the stub embedding) within 1e-6 absolute", "FAIL", ["EXP3-migration"],
      mutations=[M("EXP3-migration", exp3__migration__bbq_disk_to_hnsw__src_vs_ground_truth_max_abs=0.5)], recorder="exp3.py:check_vectors",
      verdict="verdicts.py:migration"),
    C("A.counts", 58, "document counts use `_count`", "RULE"),
    # ---- Disclosures and addendum B ----------------------------------------------------------------------------------------------
    C("B.timing", 61, "Timing disclosure", "RULE"),
    C("B.down6", 62, "The unrefreshed-partial-update test in down_6 was not a clean test", "RULE"),
    C("B.summarize", 63, "It is NOT an independent recomputation", "RULE",
      note="since review 10 the raw-sourced figures are rebuilt from typed rows (summarize.RAW_SOURCES)"),
    C("B.expiry", 64, "Whether that is identical to `start_basic`", "RULE", note="not tested"),
    C("EXP3-T4", 67, "Pass: after start_basic each form either returns the same total as on trial or a loud licence error", "REPORTED",
      note="no gate for the pass clause"),
    C("EXP3-T4.control", 67, "Control: on trial `semantic`, `match` and `match_all` must return hits > 0 (witness)", "CONTROL",
      ["EXP3-semonly-control"], witness=["semonly_trial.%s.hits" % k for k in ("semantic", "match", "match_all")]
      + ["semonly_trial.%s.status" % k for k in ("semantic", "match", "match_all")],
      recorder="exp3.py:semonly_queries", verdict="verdicts.py:semonly"),
    C("EXP3-T5", 68, "EXP3-T5 unrefreshed partial update, clean version", "REPORTED", note="no gate; n=1"),
    C("B.recompute", 69, "`recompute.py` must agree with the harness on T1 examined and mismatches, T2, T3 for down_7", "REPORTED",
      note="checked by tests (recompute_all.json); not a gate"),
    C("B.probe-start", 69, "Moment claims use the probe START time", "SUBSTITUTE", ["EXP1-T2"],
      witness=["exp1_moment.%s.first_refusal.rel_resp_s" % p for p in T2_PROBES], recorder="observe.py:moments", verdict="verdicts.py:moment",
      note="the poll END time is used: it is later than the probe start, so a refusal timed by it within 5 s is within 5 s by the start too"),
    C("B.http-patch", 70, "Every result carries `harness.http_patch`", "RULE"),
    # ---- Addendum C --------------------------------------------------------------------------------------------------------------
    C("C.T5-down", 74, "the control passes only if `n > 0` and `licence_failures / n >= 0.95`", "FAIL", ["EXP2-T5-down"],
      mutations=[M("EXP2-T5-down", exp2__T5_control_after_1s__licence_refusals=0)], recorder="observe.py:control",
      verdict="verdicts.py:licence_down"),
    C("C.T5-down-absent", 74, "A key `licence_failures` that is absent fails the gate", "FAIL", ["EXP2-T5-down"],
      mutations=[M("EXP2-T5-down", "UNVERIFIED", exp2__T5_control_after_1s__licence_failures=ABSENT)], recorder="observe.py:control",
      verdict="verdicts.py:licence_down", note="absent does not parse: UNVERIFIED (off PASS)"),
    C("C.T5-up", 75, "before start_trial `n > 0` and `licence_failures / n >= 0.95`", "FAIL", ["EXP2-T5-up"],
      mutations=[M("EXP2-T5-up", exp2__T5_control_before_start_trial__licence_refusals=0),
                 M("EXP2-T5-up", exp2__T5_control_after_trial__success=0)],
      recorder="observe.py:control", verdict="verdicts.py:licence_up"),
    C("C.T5-up-witness", 75, "after the trial `n > 0` and every control request succeeds", "PASS", ["EXP2-T5-up"],
      witness=["exp2.T5_control_before_start_trial.n", "exp2.T5_control_before_start_trial.licence_refusals",
               "exp2.T5_control_after_trial.n", "exp2.T5_control_after_trial.success"],
      recorder="observe.py:control", verdict="verdicts.py:licence_up"),
    C("C.T1-oracle", 76, "T1 passes only if `T1_backend_without_oracle == 0` and the key is present", "FAIL", ["EXP2-T1"],
      mutations=[M("EXP2-T1", exp2__T1_backend_without_oracle=1),
                 M("EXP2-T1", "UNVERIFIED", exp2__T1_backend_without_oracle=ABSENT)],
      recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("C.T1-oracle-witness", 76, "a response is \"examined\" only when it was actually compared with a reference", "PASS", ["EXP2-T1"],
      witness=["exp2.T1_backend_without_oracle"], recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("C.T4-loud", 77, "Any other 403/404 is `sem_failed_other` and fails the gate", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", exp2__writes__sem_failed=2, exp2__writes__sem_failed_loud_licence_or_missing=1, exp2__writes__sem_failed_other=1)],
      recorder="exp2.py:classify_sem_failure", verdict="verdicts.py:writes"),
    C("C.migration", 78, "scroll collection must raise on any non-200 page", "RULE", note="recorder rule (exp3.scroll_all); tested in test_gates"),
    # ---- Addendum D --------------------------------------------------------------------------------------------------------------
    C("D.T4", 82, "Its T4 status is `UNVERIFIED` and `T4_pass` is false", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", "UNVERIFIED", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=1,
                   exp2__writes__sem_failed_other=ABSENT)],
      recorder="exp2.py:verify_writes", verdict="verdicts.py:writes", note="unclassified semantic failures: UNVERIFIED, never PASS"),
    C("D.count", 83, "EXP3 independent source count", "RULE", note="recorder rule (exp3.source_count); tested in test_gates"),
)


def limits_for(gate):
    """LIMIT rows of a gate: registered clauses it cannot witness, which force its PASS to UNVERIFIED (verdicts.judge)."""
    return [c for c in CLAUSES if c.kind == "LIMIT" and gate in c.gates]


_UNGATED = {"REPORTED": "reported only: no gate decides this clause", "RULE": "procedure or recording rule: not decided by a gate",
            "LIMIT": "registered wording no recorded field witnesses"}


def clause_statuses(aggregate):
    """Every clause -> {status, reason} for summary_current.json. A gated clause takes the worst aggregate status of its gates (FAIL,
    then UNVERIFIED, then PASS); a LIMIT is UNVERIFIED; an ungated clause is UNVERIFIED (never PASS)."""
    out = {}
    for c in CLAUSES:
        if c.kind == "LIMIT":
            out[c.cid] = {"status": "UNVERIFIED", "reason": "%s: %s (forces %s off PASS)" % (_UNGATED["LIMIT"], c.note, ", ".join(c.gates) or "-")}
        elif not c.gates:
            out[c.cid] = {"status": "UNVERIFIED", "reason": _UNGATED[c.kind] + ("; " + c.note if c.note else "")}
        else:
            st = [aggregate[g]["status"] for g in c.gates]
            s = "FAIL" if "FAIL" in st else "UNVERIFIED" if any(x != "PASS" for x in st) else "PASS"
            out[c.cid] = {"status": s, "reason": "; ".join("%s %s" % (g, aggregate[g]["status"]) for g in c.gates) + (
                "; " + c.note if c.note else "")}
    return out


def augment_base(R):
    """Test support: what the review-12 recorder records on a refused DLS read and a refused polled DLS search (a status-independent
    document count of 0), added to a committed life so its DLS evidence can parse. Only for building the passing base of the generated
    tests; summarize never calls it."""
    a = R.get("exp1_after", {}).get("dls_search_as_user")
    if isinstance(a, dict):
        a["docs_in_payload"] = 0
    d = R.get("exp1_moment", {}).get("dls_search")
    if isinstance(d, dict):
        d["docs_after_transition"] = 0
    for p, v in (R.get("dls_battery_after") or {}).items():
        if isinstance(v, dict):
            v["docs_in_payload"] = 0
    return R
