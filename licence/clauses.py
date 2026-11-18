"""The clause table (gate review 12; conjuncts since review 13): THRESHOLDS.md clause by clause, starting from the REGISTERED RULES, not
from the leaves the verdicts read. One row per clause as cited (a row may hold several conditions: `conj` splits it):

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
  conj       review 13: the conjuncts (verdicts.Part ids) that together establish the clause. A clause is FAIL when one of ITS OWN
             conjuncts is VIOLATED on any life; PASS only when every conjunct of every gate deciding it is ESTABLISHED on every
             life of the campaign inventory in the gates' scope; otherwise UNVERIFIED (clause_statuses). An ungated clause has none and is UNVERIFIED. licence/CLAUSE-AUDIT.md is generated from this table
             (audit.py) with each conjunct's establishing code and the raw-row test that violates it alone (tests.test_clauses).

tests.test_clauses generates the tests from this table and fails when a THRESHOLDS.md rule line is not cited, a gate is not cited, a
FAIL clause has no mutation, or a witness is not one the gate reads. summarize.current_summary reports every row ("clauses").
"""
import collections

ABSENT = object()
KINDS = ("PASS", "FAIL", "CONTROL", "SUBSTITUTE", "LIMIT", "REPORTED", "RULE")
Clause = collections.namedtuple("Clause", "cid line quote kind gates witness mutations recorder verdict note conj")
Mutation = collections.namedtuple("Mutation", "gate edits expect")

T2_PROBES = ("rrf", "linear", "semantic_query", "inference_use", "dls_search")
BATTERY = ("get_allowed_doc0", "get_hidden_doc1", "search_match_all", "search_term_hidden", "count", "mget", "msearch", "scroll_open",
           "knn_search", "termvectors_hidden", "wildcard_index", "esql")
LATCH_CELLS = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
NOT_LICENCE_403 = 'HTTP 403: {"error":{"type":"security_exception","reason":"action [indices:data/read/search] is unauthorized"}}'


def C(cid, line, quote, kind, gates=(), witness=(), mutations=(), recorder="", verdict="", note=""):
    return Clause(cid, line, quote, kind, tuple(gates), tuple(witness), tuple(mutations), recorder, verdict, note, ())


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
    C("EXP1-T2.cadence", 24, "are polled every 50 ms across the start_basic call", "LIMIT", ["EXP1-T2"],
      recorder="observe.py:moments", verdict="verdicts.py:moment",
      note="review 13 audit: the recorder sleeps 50 ms BETWEEN polls, and one poll of seven probes takes longer; the recorded median "
           "period is about 0.19 s (poll_period_median_s), so the registered cadence is not what was run"),
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
      mutations=[M("EXP2-T4", exp2__writes__acked_absent=1)], recorder="observe.py:writes", verdict="verdicts.py:writes"),
    C("EXP2-T4.loud", 33, "every failure carries a 403 licence error or 404 for a missing endpoint, none acknowledged, none present", "FAIL",
      ["EXP2-T4"],
      mutations=[M("EXP2-T4", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=0, exp2__writes__sem_failed_other=1),
                 M("EXP2-T4", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=1, exp2__writes__sem_failed_other=0,
                   exp2__writes__sem_failed_present=1),
                 M("EXP2-T4", exp2__writes__sem_acked_absent=1)],
      recorder="observe.py:classify_sem_failure", verdict="verdicts.py:writes"),
    C("EXP2-T4.witness", 33, "every acknowledged write id is present after a final refresh", "PASS", ["EXP2-T4"],
      witness=["exp2.writes.acked", "exp2.writes.acked_absent", "exp2.writes.sem_failed"], recorder="observe.py:writes",
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
      mutations=[M("EXP3-migration", "UNVERIFIED", exp3__migration__control_corrupted_vs_true_source__vector_mismatches=0),
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
      mutations=[M("EXP2-T1", "UNVERIFIED", exp2__T1_control_broken_fallback__flagged_wrong=0, exp2__T1_control_broken_fallback__flag_rate=0.0)],
      recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("A.T1-control-witness", 54, "EXP2-T1 control (sensitivity of the wrong-answer check)", "CONTROL", ["EXP2-T1"],
      witness=["exp2.T1_control_broken_fallback.client_labelled", "exp2.T1_control_broken_fallback.flagged_wrong"],
      recorder="observe.py:correctness", verdict="verdicts.py:correctness"),
    C("A.T4-control", 55, "the write verifier is self-tested by injecting one acknowledged-but-absent id", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", "UNVERIFIED", exp2__writes__control_injected_loss_detected=False)], recorder="observe.py:writes",
      verdict="verdicts.py:writes"),
    C("A.T4-control-witness", 55, "it must report it", "CONTROL", ["EXP2-T4"], witness=["exp2.writes.control_injected_loss_detected"],
      recorder="observe.py:writes", verdict="verdicts.py:writes"),
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
      recorder="observe.py:classify_sem_failure", verdict="verdicts.py:writes"),
    C("C.migration", 78, "scroll collection must raise on any non-200 page", "RULE", note="recorder rule (exp3.scroll_all); tested in test_gates"),
    # ---- Addendum D --------------------------------------------------------------------------------------------------------------
    C("D.T4", 82, "Its T4 status is `UNVERIFIED` and `T4_pass` is false", "FAIL", ["EXP2-T4"],
      mutations=[M("EXP2-T4", "UNVERIFIED", exp2__writes__sem_failed=1, exp2__writes__sem_failed_loud_licence_or_missing=1,
                   exp2__writes__sem_failed_other=ABSENT)],
      recorder="observe.py:writes", verdict="verdicts.py:writes", note="unclassified semantic failures: UNVERIFIED, never PASS"),
    C("D.count", 83, "EXP3 independent source count", "RULE", note="recorder rule (exp3.source_count); tested in test_gates"),
)


# ---- review 13: the conjuncts of each clause (verdicts.GATES[...].conjuncts and the LIMIT rows) ----------------------------------
_T1_PASS = ("EXP2-T1.no-mismatch", "EXP2-T1.oracle", "EXP2-T1.control-readers", "EXP2-T1.control-floor")
_T2 = ("EXP2-T2.share", "EXP2-T2.run", "EXP2-T2.window", "EXP2-T2.phases")
_T4_BASIC = ("EXP2-T4.basic-attempted", "EXP2-T4.basic-loud", "EXP2-T4.basic-none-acked", "EXP2-T4.basic-none-present")
_DLS = ("EXP1-T4.control",)
_BAT = ("EXP1-T4-battery.control",)
CONJ = {
    "EXP1-T2.bound": ("EXP1-T2.refused-each", "EXP1-T2.within-5s", "EXP1-T2.answered-before"),
    "EXP1-T2.no-success": ("EXP1-T2.refused-each", "EXP1-T2.no-success-after", "EXP1-T2.observed-after"),
    "EXP1-T2.refused": ("EXP1-T2.refused-each", "EXP1-T2.answered-before", "EXP1-T2.cadence"),
    "EXP1-T2.cadence": ("EXP1-T2.cadence",),
    "EXP1-T3.detector": ("EXP1-T3.message", "EXP1-T3.detector"),
    "EXP1-T3.message": ("EXP1-T3.message", "EXP1-T3.detector", "EXP1-T3.every-cell"),
    "EXP1-T3.every-cell": ("EXP1-T3.every-cell",),
    "EXP1-T4.error": _DLS + ("EXP1-T4.refused",),
    "EXP1-T4.error-battery": _BAT + ("EXP1-T4-battery.refused",),
    "EXP1-T4.no-document": _DLS + ("EXP1-T4.no-document",),
    "EXP1-T4.no-document-battery": _BAT + ("EXP1-T4-battery.no-document",),
    "EXP1-T4.document-fails": _DLS + _BAT + ("EXP1-T4.no-document", "EXP1-T4-battery.no-document"),
    "EXP1-T4.control": _DLS,
    "EXP1-T4.control-battery": _BAT,
    "EXP2-T1.mismatch": _T1_PASS,
    "EXP2-T1.examined": ("EXP2-T1.examined",),
    "EXP2-T1.compared": _T1_PASS + ("EXP2-T1.examined", "EXP2-T1.both-labels"),
    "EXP2-T2.share": ("EXP2-T2.share", "EXP2-T2.phases"),
    "EXP2-T2.run": ("EXP2-T2.run", "EXP2-T2.window", "EXP2-T2.phases"),
    "EXP2-T2.witness": _T2,
    "EXP2-T3.down": ("EXP2-T3.down", "EXP2-T3.down-stays", "EXP2-T3.down-before"),
    "EXP2-T3.up": ("EXP2-T3.up", "EXP2-T3.up-stays", "EXP2-T3.up-before"),
    "EXP2-T3.witness": ("EXP2-T3.down", "EXP2-T3.down-stays", "EXP2-T3.down-before", "EXP2-T3.up", "EXP2-T3.up-stays", "EXP2-T3.up-before"),
    "EXP2-T4.lost": ("EXP2-T4.lost", "EXP2-T4.phases", "EXP2-T4.control"),
    "EXP2-T4.loud": _T4_BASIC,
    "EXP2-T4.witness": ("EXP2-T4.lost", "EXP2-T4.phases", "EXP2-T4.control", "EXP2-T4.basic-attempted"),
    "EXP2-T5.down": ("EXP2-T5.down", "EXP2-T5.down-window"),
    "EXP2-T5.witness": ("EXP2-T5.down", "EXP2-T5.down-window"),
    "EXP3-T2.counts": ("EXP3-T2.counts",),
    "EXP3-T2.count-fails": ("EXP3-T2.counts",),
    "EXP3-T2.vectors": ("EXP3-T2.vectors",),
    "EXP3-T2.relative": ("EXP3-T2.relative",),
    "EXP3-T2.knn": ("EXP3-T2.knn",),
    "EXP3-T2.ops": ("EXP3-T2.ops",),
    "EXP3-T2.witness": ("EXP3-T2.counts", "EXP3-T2.vectors", "EXP3-T2.knn", "EXP3-T2.ops", "EXP3-T2.relative"),
    "EXP3-T3.control": ("EXP3-T3.plain", "EXP3-T3.corrupted"),
    "EXP3-T3.witness": ("EXP3-T3.plain", "EXP3-T3.corrupted"),
    "A.T1-control": ("EXP2-T1.control-floor",),
    "A.T1-control-witness": ("EXP2-T1.control-floor",),
    "A.T4-control": ("EXP2-T4.control",),
    "A.T4-control-witness": ("EXP2-T4.control",),
    "A.ground-truth": ("EXP3.ground-truth",),
    "EXP3-T4.control": ("EXP3-T4.control",),
    "B.probe-start": ("EXP1-T2.refused-each", "EXP1-T2.within-5s"),
    "C.T5-down": ("EXP2-T5.down", "EXP2-T5.down-window"),
    "C.T5-down-absent": ("EXP2-T5.down",),
    "C.T5-up": ("EXP2-T5.up-before", "EXP2-T5.up-after", "EXP2-T5.up-window"),
    "C.T5-up-witness": ("EXP2-T5.up-before", "EXP2-T5.up-after", "EXP2-T5.up-window"),
    "C.T1-oracle": ("EXP2-T1.oracle",),
    "C.T1-oracle-witness": ("EXP2-T1.oracle",),
    "C.T4-loud": ("EXP2-T4.classified",),
    "D.T4": ("EXP2-T4.classified",),
}
CLAUSES = tuple(c._replace(conj=CONJ.get(c.cid, ())) for c in CLAUSES)

# Each conjunct: the THRESHOLDS.md line and words it establishes, the recorder function that produces its figure from raw rows, and
# what decides it. audit.py writes licence/CLAUSE-AUDIT.md from this table, the verdict source and the raw-row tests.
Conjunct = collections.namedtuple("Conjunct", "line quote recorder note")
CONJUNCTS = {
    "EXP1-T2.refused-each": Conjunct(24, "the first licence refusal", "observe.py:moments",
                                     "each mandatory probe's first refusal of its REQUIRED case (licence probes LICENCE_REFUSAL, dls_search AUTHORISATION_REFUSAL); absent: no parse"),
    "EXP1-T2.within-5s": Conjunct(24, "arrives no later than 5 s after the start_basic response", "observe.py:moments",
                                  "timed by the poll END (clause B.probe-start: later than the probe start, so conservative)"),
    "EXP1-T2.no-success-after": Conjunct(24, "no probe returns success after its first refusal", "observe.py:moments",
                                         "successes after the first refusal of any kind, by sequence position"),
    "EXP1-T2.observed-after": Conjunct(24, "no probe returns success after its first refusal", "observe.py:moments",
                                       "every poll after the first refusal observed the probe (no missing key, exception row or NOT_OBSERVED label)"),
    "EXP1-T2.answered-before": Conjunct(24, "polled every 50 ms across the start_basic call", "observe.py:moments",
                                        "answered before the start_basic request and not refused before the window: the refusal is dated by the transition"),
    "EXP1-T2.cadence": Conjunct(24, "are polled every 50 ms", "observe.py:moments", "LIMIT: never established (median period about 0.19 s)"),
    "EXP1-T3.message": Conjunct(25, "the adapter's `_is_licence_error` must be true for the error text the adapter would build", "matrix.py:verdict",
                                "recorded message and the REAL detector's answer on it (evidence._adapter_view); committed lives lack it"),
    "EXP1-T3.detector": Conjunct(25, "A false is a finding (latch would not flip)", "matrix.py:verdict", "detector false: VIOLATED"),
    "EXP1-T3.every-cell": Conjunct(25, "for every LICENCE_ERROR cell of a search path", "matrix.py:verdict", "LIMIT: three fixed cells only"),
    "EXP1-T4.control": Conjunct(26, "Control: the same search before the downgrade returns only the allowed documents", "matrix.py:dls_search",
                                "enforced in parsing: status SUCCESS, only_kw_a, returned 1..7"),
    "EXP1-T4.refused": Conjunct(26, "after start_basic a search as the DLS-restricted user must return an error", "matrix.py:dls_search",
                                "403 refusal established; 2xx VIOLATED; any other case NOT_ESTABLISHED"),
    "EXP1-T4.no-document": Conjunct(26, "returning any document is a FAIL (isolation)", "matrix.py:docs_in_payload",
                                    "documents counted by body shape whatever the status, in the after cell and every polled search started after the response"),
    "EXP1-T4-battery.control": Conjunct(26, "Control: the same search before the downgrade returns only the allowed documents", "matrix.py:dls_battery",
                                        "enforced in parsing for every read path"),
    "EXP1-T4-battery.refused": Conjunct(26, "after start_basic a search as the DLS-restricted user must return an error", "matrix.py:dls_battery",
                                        "every read path of the battery refused"),
    "EXP1-T4-battery.no-document": Conjunct(26, "returning any document is a FAIL (isolation)", "matrix.py:docs_in_payload",
                                            "every read path: no hidden document, no source, document count 0 by shape"),
    "EXP2-T1.examined": Conjunct(30, "responses examined >= 20,000 per life", "observe.py:correctness",
                                 "auto and auto+linear answers compared with a reference; without the oracle, observe.answers_bound below 20,000 is VIOLATED"),
    "EXP2-T1.no-mismatch": Conjunct(30, "Bound: 0 mismatches over all responses", "observe.py:correctness", "client: legal top-10 of the client RRF; backend: equal to the reference"),
    "EXP2-T1.both-labels": Conjunct(30, "for every response labelled client the ids equal the independently computed client RRF", "observe.py:correctness",
                                    "both the client and the backend branch were exercised (T1_examined_by_label)"),
    "EXP2-T1.oracle": Conjunct(76, "T1 passes only if `T1_backend_without_oracle == 0` and the key is present", "observe.py:correctness",
                               "a backend answer with no reference is not examined and fails"),
    "EXP2-T1.control-readers": Conjunct(30, "labelled backend, equal to the raw rrf retriever answer recorded on trial for that query", "observe.py:correctness",
                                        "the strategy=backend control readers' answers are responses labelled backend too"),
    "EXP2-T1.control-floor": Conjunct(54, "Floor: the check flags >= 90% of its client-labelled responses", "observe.py:correctness",
                                      "below the floor T1 is CLAIMED, not proven: NOT_ESTABLISHED; a wrong broken-arm backend answer is VIOLATED"),
    "EXP2-T2.share": Conjunct(31, "failed auto/lexical/vector read requests (anything not a result) <= 0.1% of requests", "observe.py:availability",
                              "auto, auto+linear, lexical and vector arms over the whole life"),
    "EXP2-T2.run": Conjunct(31, "the longest run of consecutive failed auto requests (by time, any thread) <= 2.0 s", "observe.py:consecutive_auto_failure",
                            "auto and auto+linear requests in start order; a success of any thread ends a run"),
    "EXP2-T2.window": Conjunct(31, "the longest run of consecutive failed auto requests (by time, any thread) <= 2.0 s", "observe.py:availability",
                               "stricter: failures of every read arm joined when less than 1 s apart, successes between them ignored (covers a one-thread run)"),
    "EXP2-T2.phases": Conjunct(31, "EXP2-T2 no outage", "observe.py:phases", "auto, lexical and vector requests both before the transition request and after its response"),
    "EXP2-T3.down": Conjunct(32, "first auto response labelled client no later than 5.0 s after the start_basic response", "observe.py:flip", ""),
    "EXP2-T3.down-stays": Conjunct(32, "EXP2-T3 latch down", "observe.py:flip", "stricter: no backend-labelled answer started after the first client one"),
    "EXP2-T3.down-before": Conjunct(32, "EXP2-T3 latch down", "observe.py:flip", "every auto answer completed before the request is backend-labelled, at least one"),
    "EXP2-T3.up": Conjunct(32, "first auto response labelled backend no later than probe_ttl_s + 5.0 s after the start_trial response", "observe.py:flip",
                           "ttl is the life's configuration"),
    "EXP2-T3.up-stays": Conjunct(32, "Latch up (life B)", "observe.py:flip", "stricter: no client-labelled answer started after the first backend one"),
    "EXP2-T3.up-before": Conjunct(32, "Latch up (life B)", "observe.py:flip", "every auto answer completed before the request is client-labelled, at least one"),
    "EXP2-T4.lost": Conjunct(33, "every acknowledged write id is present after a final refresh; acknowledged-but-absent = 0", "observe.py:writes",
                             "plain writers and the semantic writer"),
    "EXP2-T4.phases": Conjunct(33, "EXP2-T4 no lost write", "observe.py:writes", "plain writes acknowledged before the transition request and after its response"),
    "EXP2-T4.control": Conjunct(55, "the write verifier is self-tested by injecting one acknowledged-but-absent id; it must report it", "observe.py:writes",
                                "as registered: the set difference reports an injected id (it does not test the read-back itself); not met: NOT_ESTABLISHED"),
    "EXP2-T4.basic-attempted": Conjunct(33, "The semantic_text writer fails loudly on Basic", "observe.py:writes",
                                        "semantic writes issued on Basic exist (down: started at or after the response; up: completed before the request)"),
    "EXP2-T4.basic-loud": Conjunct(33, "every failure carries a 403 licence error or 404 for a missing endpoint", "observe.py:classify_sem_failure", "Basic writes"),
    "EXP2-T4.basic-none-acked": Conjunct(33, "none acknowledged", "observe.py:writes", "Basic writes"),
    "EXP2-T4.basic-none-present": Conjunct(33, "none present", "observe.py:writes", "Basic writes"),
    "EXP2-T4.classified": Conjunct(77, "Any other 403/404 is `sem_failed_other` and fails the gate", "observe.py:classify_sem_failure",
                                   "whole life (addendum C/D); also no failed semantic write present (stricter)"),
    "EXP2-T5.down": Conjunct(34, "arm strategy=backend after start_basic: >= 95% of requests (after the first 1 s) fail with the licence error",
                             "observe.py:control", "LICENCE_REFUSAL Outcome case (403 with the licence text)"),
    "EXP2-T5.down-window": Conjunct(34, "(after the first 1 s)", "observe.py:control", "the figure records its window start; anything but 1 s does not count"),
    "EXP2-T5.up-before": Conjunct(75, "before start_trial `n > 0` and `licence_failures / n >= 0.95`", "observe.py:control", "requests completed before the request"),
    "EXP2-T5.up-after": Conjunct(75, "after the trial `n > 0` and every control request succeeds", "observe.py:control",
                                 "every control request STARTED at or after the response (review 13 F31)"),
    "EXP2-T5.up-window": Conjunct(75, "after the trial `n > 0` and every control request succeeds", "observe.py:control",
                                  "the figure records its window start; anything but 0 s does not count"),
    "EXP3-T2.counts": Conjunct(41, "doc counts equal source (300/300 and 200/200)", "exp3.py:migrate_vectors", "substitute: equal to the independent _count"),
    "EXP3-T2.vectors": Conjunct(41, "vectors equal element-wise to the stored source vectors", "exp3.py:check_vectors", ""),
    "EXP3-T2.knn": Conjunct(41, "kNN self-hit top-1 >= 99% on the migrated index", "exp3.py:check_vectors", ""),
    "EXP3-T2.ops": Conjunct(41, "the migrated index passes every Basic-available operation", "exp3.py:migrate_vectors", ""),
    "EXP3-T2.relative": Conjunct(41, "within 1% relative of the stub's float vectors", "exp3.py:check_semantic", "LIMIT: not recorded"),
    "EXP3.ground-truth": Conjunct(57, "the ground truth (the stub embedding) within 1e-6 absolute", "exp3.py:check_vectors", ""),
    "EXP3-T3.plain": Conjunct(42, "the same recipe applied to the plain index changes nothing (counts and vectors equal)", "exp3.py:migrate_vectors", ""),
    "EXP3-T3.corrupted": Conjunct(42, "a deliberately corrupted migration (one vector perturbed) must be caught by the vector-equality check",
                                  "exp3.py:migrate_vectors", "not caught: NOT_ESTABLISHED"),
    "EXP3-T4.control": Conjunct(67, "Control: on trial `semantic`, `match` and `match_all` must return hits > 0 (witness)", "exp3.py:semonly_queries", ""),
}


def limits_for(gate):
    """LIMIT rows of a gate: registered clauses it cannot witness, which force its PASS to UNVERIFIED (verdicts.judge)."""
    return [c for c in CLAUSES if c.kind == "LIMIT" and gate in c.gates]


def clause_statuses(lives_parts):
    """Every clause -> {status, reason} for summary_current.json. lives_parts: {gate: [(life file, {conjunct id: (state, reason)} or
    None when the life is absent)]} over the campaign inventory. A clause is FAIL when one of ITS OWN conjuncts is VIOLATED on any life
    (a violation is attributed only to the clause that states it); PASS only when every conjunct of every gate deciding it is
    ESTABLISHED on every in-scope life (a clause never passes on a gate that does not); otherwise UNVERIFIED. An ungated clause is
    UNVERIFIED (never PASS)."""
    out = {}
    for c in CLAUSES:
        if not c.conj:
            out[c.cid] = {"status": "UNVERIFIED", "reason": {"REPORTED": "reported only: no gate decides this clause",
                                                             "RULE": "procedure or recording rule: not decided by a gate"}.get(c.kind, c.kind)
                          + ("; " + c.note if c.note else "")}
            continue
        seen = {"VIOLATED": [], "NOT_ESTABLISHED": [], "ESTABLISHED": []}
        for g in c.gates:
            for life, parts in lives_parts.get(g, []):
                for cid in c.conj:
                    if parts is None:
                        seen["NOT_ESTABLISHED"].append("%s: life file absent" % life)
                    elif cid in parts:
                        st, why = parts[cid]
                        seen[st].append("%s %s" % (life, why) if st != "ESTABLISHED" else life)
        gate_open = sorted({"%s %s" % (life, cid) for g in c.gates for life, parts in lives_parts.get(g, [])
                            for cid, (st, _) in (parts or {}).items() if st != "ESTABLISHED" and cid not in c.conj})
        st = "FAIL" if seen["VIOLATED"] else "UNVERIFIED" if (seen["NOT_ESTABLISHED"] or not seen["ESTABLISHED"] or gate_open) else "PASS"
        if not seen["VIOLATED"] and not seen["NOT_ESTABLISHED"] and gate_open:
            seen["NOT_ESTABLISHED"] = ["the gate's other conjuncts are not all established: " + ", ".join(gate_open[:8])]
        bad = seen["VIOLATED"] or seen["NOT_ESTABLISHED"]
        n = len(seen["ESTABLISHED"]) + len(seen["VIOLATED"]) + len(seen["NOT_ESTABLISHED"])
        reason = "conjuncts %s: %d of %d life-conjunct checks established" % (", ".join(c.conj), len(seen["ESTABLISHED"]), n)
        if bad:
            reason += "; %s: %s" % ("violated" if seen["VIOLATED"] else "not established", " | ".join(sorted(set(bad))[:6])[:900])
        out[c.cid] = {"status": st, "reason": reason + ("; " + c.note if c.note else "")}
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
