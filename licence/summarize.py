"""Threshold reapplication: applies the THRESHOLDS.md gates to the figures the harness recorded in the per-life result JSON files.
It does not recompute those figures from raw rows; recompute.py does that, and only where raw rows were saved (T2, T3, T5 for all lives;
T1 and the polling moments for down_7 only; see DEGRADE.md "Evidence status and limits"). Usage: summarize.py RESULTS_DIR [out.json]
Reads only the result files; does not import the experiment code.

Coverage rule (gate reviews 5 and 6): every gate is declared in GATES with every input it reads and that input's kind; gate() is the only
producer of a gate status. An input that is absent, None, of the wrong type or outside its kind (for example a count of 0 where a count
must have been observed) gives UNVERIFIED before the rule runs; the rule then returns PASS only on the intended observation. A status
outside the intended set is FAIL where it is an observed failure of the operation under test (a 5xx or transport 0 on a migrated index)
and UNVERIFIED where it says nothing about it (a 401, 5xx or 0 where a DLS refusal was expected). tests/test_gates.py GatePropertyTests
mutates every declared input of every gate. The per-life reports outside GATES (matrix table, exp3 operation lists, exp4) are reports,
not gates; they may raise on a malformed life file."""
import collections
import glob
import gzip
import json
import os
import sys


def wilson_lower(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (c - r) / d


def _backfill_up_licence_failures(R, path):
    """Lives recorded before addendum C kept only the total failure count of the pre-trial control. Recount licence failures
    ("non-compliant" in the error text) from the committed raw rows, so the gate can use licence_failures / n."""
    if R.get("mode") != "up" or "exp2" not in R:
        return
    pre = R["exp2"].get("T5_control_before_start_trial")
    rows_path = path[:-5] + ".rows.gz"
    if pre is None or "licence_failures" in pre or not os.path.exists(rows_path):
        return
    t_req = R["start_trial"]["t_req"]
    rows = [json.loads(l) for l in gzip.open(rows_path, "rt")]
    ctl = [r for r in rows if r[2] == "ctl" and r[1] < t_req]
    if len(ctl) == pre["n"]:   # only trust the recount when it reproduces the recorded denominator
        pre["licence_failures"] = sum(1 for r in ctl if not r[4] and "non-compliant" in (r[7] or ""))
        pre["licence_failures_source"] = "recounted from rows.gz"


def load(d, mode):
    out = []
    for f in sorted(glob.glob(os.path.join(d, "%s_[0-9]*.json" % mode))):
        R = json.load(open(f))
        R["_file"] = os.path.basename(f)
        _backfill_up_licence_failures(R, f)
        out.append(R)
    return out


T2_PROBES = ("rrf", "linear", "semantic_query", "inference_use", "dls_search")   # THRESHOLDS.md EXP1-T2: all five licensed probes are mandatory
BATTERY = ("get_allowed_doc0", "get_hidden_doc1", "search_match_all", "search_term_hidden", "count", "mget", "msearch", "scroll_open", "knn_search",
           "termvectors_hidden", "wildcard_index", "esql")
LATCH_CELLS = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
MIG_INDICES = ("lic3-bbq_disk-mig", "lic3-sem-mig")
MIG_OPS = ("update", "delete", "force_merge", "index_new")
OK2XX = frozenset(range(200, 300))

# ---- the gate table. EVERY gate this module reports is declared here with EVERY input it reads; gate() is the only function that
# produces a gate status, and an input that is absent, None, of the wrong type or outside its kind gives UNVERIFIED before the gate's
# rule runs. Kinds (each tested by tests/test_gates.py GatePropertyTests with the mutations listed there):
#   status  an HTTP status (int); `expect` is the set that counts as the intended observation, the rule decides what anything else means
#   pos     an int > 0 (a count that must have been observed)
#   zero    an int that the rule requires to be 0
#   one     an int that the rule requires to be 1
#   measure a number (int or float); `bad` is a value the rule must reject
#   flag    a bool; `expect` is the value the rule requires
#   text    a non-empty str or dict (an error body)
#   optzero absent, or an int that must be 0 (needed by the rule only in some cases; the rule returns UNVERIFIED if it needs it and it is absent)
Input = collections.namedtuple("Input", "path kind expect bad")
Gate = collections.namedtuple("Gate", "scope out inputs rule doc")


def I(path, kind, expect=None, bad=None):
    return Input(tuple(path.split(".")), kind, expect, bad)


MISSING = object()


def fetch(R, path):
    v = R
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return MISSING
        v = v[k]
    return v


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def well_formed(inp, v):
    k = inp.kind
    if k == "optzero":
        return v is MISSING or _is_int(v)
    if v is MISSING:
        return False
    if v is None:
        return False
    if k in ("status", "zero", "one"):
        return _is_int(v)
    if k == "pos":
        return _is_int(v) and v > 0
    if k == "measure":
        return (_is_int(v) or isinstance(v, float)) and v == v
    if k == "flag":
        return isinstance(v, bool)
    if k == "text":
        return isinstance(v, (str, dict)) and len(v) > 0
    raise ValueError("unknown input kind %s" % k)


def combine(statuses):
    """FAIL if any input failed; else UNVERIFIED if any is unverified (or there are none); else PASS."""
    s = list(statuses)
    return "FAIL" if "FAIL" in s else ("UNVERIFIED" if not s or any(x != "PASS" for x in s) else "PASS")


def _status_rule(ok, observed):
    return "PASS" if ok else ("FAIL" if observed else "UNVERIFIED")


# rules: v(path_string) -> validated value; return PASS / FAIL / UNVERIFIED
def _r_exp1_t2(v):
    return combine("PASS" if v("exp1_moment.%s.first_refusal_rel_resp_s" % p) <= 5.0 and v("exp1_moment.%s.success_after_first_refusal" % p) == 0
                   else "FAIL" for p in T2_PROBES)


def _r_exp1_t3(v):
    return combine("PASS" if v("exp1_after.%s.status" % c) == 403 and "current license is non-compliant" in json.dumps(v("exp1_after.%s.error" % c)).lower()
                   else "FAIL" for c in LATCH_CELLS)


def _dls_control(v):
    """Positive restricted control before the transition: the DLS user read the permitted documents only (4 of 8, all kw a)."""
    return v("exp1_trial.dls_search_as_user.status") == 200 and v("exp1_trial.dls_search_as_user.only_kw_a") is True \
        and 0 < v("exp1_trial.dls_search_as_user.returned") < 8


def _refusal(status, leaked):
    """After the transition: 403 with nothing returned is the fail-closed refusal; a 2xx or a returned document is a FAIL; anything else
    (401 authentication, 404, 5xx, transport 0) says nothing about DLS: UNVERIFIED."""
    if leaked or status in OK2XX:
        return "FAIL"
    return "PASS" if status == 403 else "UNVERIFIED"


def _r_exp1_t4(v):
    if not _dls_control(v):
        return "UNVERIFIED"
    if v("exp1_after.dls_search_as_user.class") in ("UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS", "SKIPPED"):
        return "UNVERIFIED"
    return _refusal(v("exp1_after.dls_search_as_user.status"), False)   # any 2xx is already a FAIL; a refusal carries no hits


def _r_exp1_t4_battery(v):
    if not _dls_control(v):
        return "UNVERIFIED"
    for p in BATTERY:   # trial control: each read path answered as DLS should (allowed doc readable, hidden doc not found, nothing hidden leaked)
        want = 404 if p == "get_hidden_doc1" else 200
        if v("dls_battery_trial.%s.status" % p) != want or v("dls_battery_trial.%s.returned_hidden_doc" % p):
            return "UNVERIFIED"
    if not (v("dls_battery_trial.get_allowed_doc0.returned_any_source") and v("dls_battery_trial.search_match_all.returned_any_source")):
        return "UNVERIFIED"
    return combine(_refusal(v("dls_battery_after.%s.status" % p), v("dls_battery_after.%s.returned_hidden_doc" % p) or v("dls_battery_after.%s.returned_any_source" % p))
                   for p in BATTERY)


def _r_exp2_t1(v):
    ok = (v("exp2.T1_examined") >= 20000 and v("exp2.T1_mismatches") == 0 and v("exp2.T1_backend_without_oracle") == 0
          and v("exp2.T1_control_broken_fallback.flag_rate") >= 0.9 and v("exp2.T1_control_broken_fallback.backend_wrong") == 0)
    return "PASS" if ok else "FAIL"


def _r_exp2_t2(v):
    frac = v("exp2.T2_failed_requests") / v("exp2.T2_non_control_requests")
    return "PASS" if frac <= 0.001 and v("exp2.T2_longest_failure_window_s") <= 2.0 else "FAIL"


def _r_exp2_t3_down(v):
    return "PASS" if v("exp2.T3_flip_latency_s") <= 5.0 and v("exp2.T3_backend_after_first_client") == 0 else "FAIL"


def _r_exp2_t3_up(v):
    return "PASS" if v("exp2.T3_flip_latency_s") <= v("ttl") + 5.0 and v("exp2.T3_client_after_first_backend") == 0 else "FAIL"


def _r_exp2_t4(v):
    w = lambda k: v("exp2.writes." + k)
    if not (w("acked_absent") == 0 and w("sem_acked_absent") == 0 and w("sem_failed_present") == 0 and w("control_injected_loss_detected") is True):
        return "FAIL"
    if w("sem_failed") == 0:
        return "PASS"   # no semantic write failed: nothing to classify
    other = w("sem_failed_other")
    if other is MISSING:
        return "UNVERIFIED"   # failures exist but were never classified under addendum C (addendum D)
    return "PASS" if other == 0 and w("sem_failed") == w("sem_failed_loud_licence_or_missing") else "FAIL"


def _r_exp2_t5_down(v):
    return "PASS" if v("exp2.T5_control_after_1s.licence_failures") / v("exp2.T5_control_after_1s.n") >= 0.95 else "FAIL"


def _r_exp2_t5_up(v):
    pre, post = "exp2.T5_control_before_start_trial.", "exp2.T5_control_after_trial."
    return "PASS" if v(pre + "licence_failures") / v(pre + "n") >= 0.95 and v(post + "success") == v(post + "n") else "FAIL"


def _r_exp3_migration(v):
    m = lambda k: v("exp3.migration." + k)
    ind = lambda k: v("exp3.after_restart.counts_before." + k)
    indep = (m("bbq_disk_to_hnsw.src_count") == ind("bbq_disk") and m("bbq_disk_to_bbq_hnsw.src_count") == ind("bbq_disk")
             and m("semantic_to_text_vector.docs") + m("semantic_to_text_vector.skipped_not_single_chunk") == ind("sem")
             and m("control_plain_to_hnsw.src_count") == ind("plain"))
    parts = [
        m("bbq_disk_to_hnsw.dst_count") == m("bbq_disk_to_hnsw.src_count") and m("bbq_disk_to_hnsw.vector_mismatches") == 0
        and m("bbq_disk_to_hnsw.knn_self_top1") / m("bbq_disk_to_hnsw.knn_self_of") >= 0.99 and m("bbq_disk_to_hnsw.src_vs_ground_truth_max_abs") <= 1e-6,
        m("bbq_disk_to_bbq_hnsw.dst_count") == m("bbq_disk_to_bbq_hnsw.src_count") and m("bbq_disk_to_bbq_hnsw.vector_mismatches") == 0
        and m("bbq_disk_to_bbq_hnsw.knn_self_top1") / m("bbq_disk_to_bbq_hnsw.knn_self_of") >= 0.99,
        m("semantic_to_text_vector.dst_count") == m("semantic_to_text_vector.docs") and m("semantic_to_text_vector.vector_mismatches_vs_stored") == 0
        and m("semantic_to_text_vector.worst_abs_err_vs_stub") <= 0.005 and m("semantic_to_text_vector.worst_cosine_vs_stub") >= 0.9999
        and m("semantic_to_text_vector.knn_self_top1") / m("semantic_to_text_vector.knn_self_of") >= 0.99 and m("semantic_to_text_vector.skipped_not_single_chunk") == 0,
        m("control_plain_to_hnsw.vector_mismatches") == 0 and m("control_plain_to_hnsw.dst_count") == m("control_plain_to_hnsw.src_count"),
        m("control_corrupted_vs_true_source.vector_mismatches") == 1,
        indep,
        # EXP3-T2: every Basic operation on every migrated index answered 2xx (a 0 transport sentinel or a 5xx is an observed failure)
        all(m("migrated_basic_ops.%s.%s" % (i, o)) in OK2XX for i in MIG_INDICES for o in MIG_OPS),
    ]
    return "PASS" if all(parts) else "FAIL"


def _r_exp3_semonly(v):
    return "PASS" if all(v("semonly_trial.%s.status" % k) == 200 for k in ("semantic", "match", "match_all")) else "FAIL"


def _mig_inputs():
    out = []
    for pre in ("bbq_disk_to_hnsw", "bbq_disk_to_bbq_hnsw"):
        out += [I("exp3.migration.%s.src_count" % pre, "pos"), I("exp3.migration.%s.dst_count" % pre, "pos"),
                I("exp3.migration.%s.vector_mismatches" % pre, "zero"), I("exp3.migration.%s.knn_self_top1" % pre, "pos"),
                I("exp3.migration.%s.knn_self_of" % pre, "pos")]
    s = "exp3.migration.semantic_to_text_vector."
    out += [I("exp3.migration.bbq_disk_to_hnsw.src_vs_ground_truth_max_abs", "measure", bad=0.5),
            I(s + "docs", "pos"), I(s + "dst_count", "pos"), I(s + "vector_mismatches_vs_stored", "zero"), I(s + "worst_abs_err_vs_stub", "measure", bad=0.5),
            I(s + "worst_cosine_vs_stub", "measure", bad=0.5), I(s + "knn_self_top1", "pos"), I(s + "knn_self_of", "pos"), I(s + "skipped_not_single_chunk", "zero"),
            I("exp3.migration.control_plain_to_hnsw.vector_mismatches", "zero"), I("exp3.migration.control_plain_to_hnsw.dst_count", "pos"),
            I("exp3.migration.control_plain_to_hnsw.src_count", "pos"), I("exp3.migration.control_corrupted_vs_true_source.vector_mismatches", "one")]
    out += [I("exp3.after_restart.counts_before.%s" % k, "pos") for k in ("bbq_disk", "sem", "plain")]
    out += [I("exp3.migration.migrated_basic_ops.%s.%s" % (i, o), "status", expect=OK2XX) for i in MIG_INDICES for o in MIG_OPS]
    return out


_DLS_CONTROL = [I("exp1_trial.dls_search_as_user.status", "status", expect={200}), I("exp1_trial.dls_search_as_user.only_kw_a", "flag", expect=True),
                I("exp1_trial.dls_search_as_user.returned", "pos", bad=8)]

GATES = {
    "EXP1-T2": Gate("down", ("exp1", "T2"), [I("exp1_moment.%s.%s" % (p, f), k, bad=b) for p in T2_PROBES
                                            for f, k, b in (("first_refusal_rel_resp_s", "measure", 9.0), ("success_after_first_refusal", "zero", None), ("n", "pos", None))],
                    _r_exp1_t2, "first licence refusal of every licensed probe within 5 s of start_basic, no success after it"),
    "EXP1-T3": Gate("down", ("exp1", "T3_latch"), [x for c in LATCH_CELLS for x in (I("exp1_after.%s.status" % c, "status", expect={403}), I("exp1_after.%s.error" % c, "text"))],
                    _r_exp1_t3, "every licensed search path refuses with 403 and the non-compliant licence text the adapter latches on"),
    "EXP1-T4": Gate("down", ("exp1", "T4"), _DLS_CONTROL + [I("exp1_after.dls_search_as_user.status", "status", expect={403}),
                                                           I("exp1_after.dls_search_as_user.class", "text", bad="UNVERIFIED_PREREQUISITE")],
                    _r_exp1_t4, "positive restricted control on trial; after start_basic the DLS user's search is refused with 403 (a 2xx is FAIL; 401, 5xx or 0 is UNVERIFIED; the after cell records no hit count)"),
    "EXP1-T4-battery": Gate("down", ("exp1", "T4_battery"),
                            _DLS_CONTROL + [x for p in BATTERY for x in (
                                I("dls_battery_trial.%s.status" % p, "status", expect={404} if p == "get_hidden_doc1" else {200}),
                                I("dls_battery_trial.%s.returned_hidden_doc" % p, "flag", expect=False),
                                I("dls_battery_after.%s.status" % p, "status", expect={403}),
                                I("dls_battery_after.%s.returned_hidden_doc" % p, "flag", expect=False),
                                I("dls_battery_after.%s.returned_any_source" % p, "flag", expect=False))]
                            + [I("dls_battery_trial.get_allowed_doc0.returned_any_source", "flag", expect=True),
                               I("dls_battery_trial.search_match_all.returned_any_source", "flag", expect=True)],
                            _r_exp1_t4_battery, "the same control, then every one of 12 read paths refused with 403 and no document after start_basic"),
    "EXP2-T1": Gate("both", ("exp2", "T1"), [I("exp2.T1_examined", "pos", bad=19999), I("exp2.T1_mismatches", "zero"), I("exp2.T1_backend_without_oracle", "zero"),
                                             I("exp2.T1_control_broken_fallback.flag_rate", "measure", bad=0.5), I("exp2.T1_control_broken_fallback.backend_wrong", "zero")],
                    _r_exp2_t1, "0 wrong answers over >= 20,000 examined, every backend answer had an oracle, control flagged"),
    "EXP2-T2": Gate("both", ("exp2", "T2"), [I("exp2.T2_failed_requests", "measure", bad=10 ** 9), I("exp2.T2_non_control_requests", "pos"),
                                             I("exp2.T2_longest_failure_window_s", "measure", bad=9.0)],
                    _r_exp2_t2, "failed reads <= 0.1% and the longest failure window <= 2 s"),
    "EXP2-T3-down": Gate("down", ("exp2", "T3"), [I("exp2.T3_flip_latency_s", "measure", bad=9.0), I("exp2.T3_backend_after_first_client", "zero")],
                         _r_exp2_t3_down, "first client-labelled answer within 5 s of start_basic, no backend label after it"),
    "EXP2-T3-up": Gate("up", ("exp2", "T3"), [I("exp2.T3_flip_latency_s", "measure", bad=10 ** 6), I("ttl", "measure", bad=-10.0), I("exp2.T3_client_after_first_backend", "zero")],
                       _r_exp2_t3_up, "first backend-labelled answer within probe TTL + 5 s of start_trial, no client label after it"),
    "EXP2-T4": Gate("both", ("exp2", "T4"), [I("exp2.writes.acked", "pos"), I("exp2.writes.acked_absent", "zero"), I("exp2.writes.sem_acked", "pos"),
                                             I("exp2.writes.sem_acked_absent", "zero"), I("exp2.writes.sem_failed_present", "zero"),
                                             I("exp2.writes.control_injected_loss_detected", "flag", expect=True),
                                             I("exp2.writes.sem_failed", "measure", bad=10 ** 6), I("exp2.writes.sem_failed_loud_licence_or_missing", "measure", bad=-1),
                                             I("exp2.writes.sem_failed_other", "optzero")],
                    _r_exp2_t4, "no acknowledged write lost and every semantic write failure classified loud (addendum C)"),
    "EXP2-T5-down": Gate("down", ("exp2", "T5"), [I("exp2.T5_control_after_1s.n", "pos"), I("exp2.T5_control_after_1s.licence_failures", "measure", bad=0)],
                         _r_exp2_t5_down, ">= 95% of control requests after 1 s fail with the licence error"),
    "EXP2-T5-up": Gate("up", ("exp2", "T5"), [I("exp2.T5_control_before_start_trial.n", "pos"), I("exp2.T5_control_before_start_trial.licence_failures", "measure", bad=0),
                                              I("exp2.T5_control_after_trial.n", "pos"), I("exp2.T5_control_after_trial.success", "measure", bad=0)],
                       _r_exp2_t5_up, ">= 95% licence failures before start_trial and every control request succeeds after it"),
    "EXP3-migration": Gate("down", ("exp3", "migration"), _mig_inputs(), _r_exp3_migration,
                           "every migrated copy complete and equal to the source (independent counts, controls) and EXP3-T2: Basic operations on the migrated indices 2xx"),
    "EXP3-semonly-control": Gate("down", ("exp3", "semonly_control"), [x for k in ("semantic", "match", "match_all") for x in
                                                                       (I("semonly_trial.%s.status" % k, "status", expect={200}), I("semonly_trial.%s.hits" % k, "pos"))],
                                 _r_exp3_semonly, "the semantic-only fixture answers semantic, match and match_all with hits on trial"),
}


def gate(name, R):
    """The ONLY producer of a gate status. Inputs are validated against the table first: any absent or malformed input gives UNVERIFIED."""
    g = GATES[name]
    vals = {}
    for inp in g.inputs:
        x = fetch(R, inp.path)
        if not well_formed(inp, x):
            return "UNVERIFIED"
        vals[".".join(inp.path)] = x

    def v(p):
        return vals[p]   # a rule reading an undeclared input raises KeyError: caught by the property test
    return g.rule(v)


def put(r, name, R):
    """Write gate `name` into result dict r as <out>_status and <out>_pass (pass only for PASS)."""
    s = gate(name, R)
    out = GATES[name].out[1]
    r[out + "_status"], r[out + "_pass"] = s, s == "PASS"
    return s


def exp1_checks(R):
    r = {}
    m = R.get("exp1_moment") if isinstance(R.get("exp1_moment"), dict) else {}
    r["T2_first_refusal_s"] = {f: v.get("first_refusal_rel_resp_s") for f, v in m.items() if isinstance(v, dict)}
    r["T2_dls_returned_before"] = (m.get("dls_search") or {}).get("dls_returned_counts_before") if isinstance(m.get("dls_search"), dict) else None
    for name in ("EXP1-T2", "EXP1-T3", "EXP1-T4", "EXP1-T4-battery"):
        put(r, name, R)
    after = R.get("exp1_after") if isinstance(R.get("exp1_after"), dict) else {}
    d = after.get("dls_search_as_user") if isinstance(after.get("dls_search_as_user"), dict) else {}
    r["T4_dls_after"] = {"status": d.get("status"), "class": d.get("class"), "returned": d.get("returned"), "reason": str((d.get("error") or {}).get("reason", ""))[:90]}
    r["T4_control_before_returned"] = r["T2_dls_returned_before"]
    a, b = R.get("dls_battery_after"), R.get("dls_battery_trial")
    if isinstance(a, dict) and isinstance(b, dict):
        r["T4_battery_after_statuses"] = {k: v.get("status") for k, v in a.items() if isinstance(v, dict)}
        r["T4_battery_trial_statuses"] = {k: v.get("status") for k, v in b.items() if isinstance(v, dict)}
    return r


def matrix_table(R):
    cols = {"basic0": R.get("exp1_basic0"), "trial": R.get("exp1_trial"), "after": R.get("exp1_after")}
    cells = sorted(set().union(*[set(c) for c in cols.values() if c]))
    t = {}
    for cell in cells:
        row = {}
        for k, c in cols.items():
            v = (c or {}).get(cell)
            if v is None:
                row[k] = None
            elif "class" in v:
                row[k] = v["class"] + ("/%s" % v.get("status") if v.get("status") else "")
            else:  # composite cells
                row[k] = {kk: (vv.get("class") if isinstance(vv, dict) else vv) for kk, vv in v.items()}
        t[cell] = row
    return t


def _g(R, path):
    v = fetch(R, tuple(path.split(".")))
    return None if v is MISSING else v


def _num(x):
    return (_is_int(x) or isinstance(x, float)) and x == x


def exp2_checks(R, direction):
    """Report figures (None where not recorded) plus the EXP2 gates from the table. Never raises on a missing or malformed field."""
    g = lambda p: _g(R, p)
    failed, n = g("exp2.T2_failed_requests"), g("exp2.T2_non_control_requests")
    r = {"T1_examined": g("exp2.T1_examined"), "T1_mismatches": g("exp2.T1_mismatches"), "T1_control_flag_rate": g("exp2.T1_control_broken_fallback.flag_rate"),
         "T1_control_n": g("exp2.T1_control_broken_fallback.client_labelled"), "T1_control_backend_wrong": g("exp2.T1_control_broken_fallback.backend_wrong"),
         "T1_backend_without_oracle": g("exp2.T1_backend_without_oracle"),
         "T2_fail_fraction": failed / n if _num(failed) and _is_int(n) and n > 0 else None, "T2_longest_window_s": g("exp2.T2_longest_failure_window_s"),
         "T3_flip_latency_s": g("exp2.T3_flip_latency_s"), "ttl": g("ttl"), "T6_rrf": g("exp2.T6_rrf_backend_vs_client_top10"),
         "T6_linear": g("exp2.T6_linear_backend_vs_client_rrf_top10")}
    r["T4"] = {k: g("exp2.writes." + k) for k in ("acked", "acked_absent", "present_unacked", "failed_batches", "sem_acked", "sem_acked_absent", "sem_failed",
                                                   "sem_failed_loud_licence_or_missing", "sem_failed_present", "control_injected_loss_detected")}
    if direction == "down":
        r["T5"] = {k: g("exp2.T5_control_after_1s." + k) for k in ("n", "fail_fraction", "licence_failures")}
    else:
        r["T5"] = {"before_n": g("exp2.T5_control_before_start_trial.n"), "before_failures": g("exp2.T5_control_before_start_trial.failures"),
                   "before_licence_failures": g("exp2.T5_control_before_start_trial.licence_failures"), "after_trial_n": g("exp2.T5_control_after_trial.n"),
                   "after_trial_success": g("exp2.T5_control_after_trial.success")}
        r["sem_first_ok_after_trial_s"] = g("exp2.sem_first_ok_after_trial_s")
    w = r["T4"]
    # report, not a gate: the pre-addendum-C rule (every semantic failure with any 403/404 body counted loud), kept for comparison
    r["T4_legacy_rule_result"] = (w["acked_absent"] == 0 and w["sem_acked_absent"] == 0 and w["sem_failed_present"] == 0 and w["control_injected_loss_detected"] is True
                                  and w["sem_failed"] == w["sem_failed_loud_licence_or_missing"]) if all(x is not None for x in w.values()) else None
    for name in ("EXP2-T1", "EXP2-T2", "EXP2-T3-" + direction, "EXP2-T4", "EXP2-T5-" + direction):
        put(r, name, R)
    return r


def exp3_checks(R):
    E = R["exp3"]
    out = {"ops": {}, "silent": [], "reads_diff": []}
    for kind in ("sem", "bbq_disk", "bbq_hnsw", "plain"):
        tm, am = E["twin_mutations"][kind], E["after"]["mutations"][kind]
        for op, t in tm.items():
            a = am[op]
            if op == "bulk_partial_update":
                tr = "ok" if t["summary"]["failed"] == 0 else "fail"
                ba = "ok" if a["summary"]["failed"] == 0 else ("LOUD_LICENCE_ITEM" if "non-compliant" in json.dumps(a["summary"]["first_error"]) else "FAIL_OTHER")
                same = (tr == ba == "ok" and t["effect"] == a["effect"])
                verdict = "WORKS" if same else ba
                if tr == "ok" and ba == "ok" and not same:
                    out["silent"].append([kind, op, t["effect"], a["effect"]])
                out["ops"].setdefault(kind, {})[op] = {"trial": tr + "/http%s" % t["status"], "basic": verdict + "/http%s" % a["status"]}
                continue
            if a["status"] < 300:
                same = t["effect"] == a["effect"] and t["status"] < 300
                if not same:
                    out["silent"].append([kind, op, t.get("effect"), a.get("effect")])
                verdict = "WORKS" if same else "SILENT_DIFFERENCE"
            else:
                verdict = "LOUD_LICENCE" if a["class"] == "LICENCE_ERROR" else "FAIL_OTHER(%s)" % a["class"]
                # a loud failure must leave data intact: effect fields equal pre-op values (tag unchanged, embedding present)
            out["ops"].setdefault(kind, {})[op] = {"trial": t["class"] + "/%s" % t["status"], "basic": verdict + "/%s" % a["status"]}
        pre, aft = E["pre_reads"][kind], E["after"]["reads"][kind]
        for k in pre:
            if k in ("lexical_title", "match_on_semantic_field", "semantic_query", "query_string_on_all"):
                if aft[k].get("class") == "AVAILABLE" and aft[k].get("hits") != pre[k].get("hits"):
                    out["reads_diff"].append([kind, k, pre[k].get("hits"), aft[k].get("hits")])
                if aft[k].get("class") != pre[k].get("class"):
                    out["reads_diff"].append([kind, k, pre[k].get("class"), aft[k].get("class"), (aft[k].get("error") or {}).get("reason")])
            elif k in ("get_all", "count", "inference_embeddings_sample", "knn_self_top1", "vectors_equal_ground_truth"):
                if json.dumps(pre[k], sort_keys=True) != json.dumps(aft[k], sort_keys=True) and kind != "x":
                    out["reads_diff"].append([kind, k, pre[k], aft[k]])
    chk = {}
    if isinstance(E.get("migration"), dict) and "error" in E["migration"]:
        chk["error"] = E["migration"]["error"]
    put(chk, "EXP3-migration", R)   # EXP3 migration and EXP3-T2 (Basic operations on the migrated indices): one gate from the table
    chk["migrated_basic_ops"] = _g(R, "exp3.migration.migrated_basic_ops")
    out["migration_checks"] = chk
    if "unrefreshed_twin_on_trial" in R:
        u = lambda x: {"searchable_before_update": x.get("searchable_before_update", "not recorded (fixture was refreshed by a restart: not a clean test)"), "bulk_failed": x["bulk"]["failed"], "bulk_http": x["bulk_status"],
                       "tags": x["tags"], "embeddings": x["embeddings"], "semantic_search": x["semantic_search"].get("status"), "count": x["count"]}
        bs = R.get("unrefreshed_after_start_basic") or E["after"].get("unrefreshed_partial_on_basic")
        out["unrefreshed_partial"] = {"trial": u(R["unrefreshed_twin_on_trial"]), "basic": u(bs)}
    if "semonly_trial" in R:
        sm = {}
        for k, t in R["semonly_trial"].items():
            a = R["semonly_after"][k]
            sm[k] = {"trial": [t["status"], t["hits"]], "basic": [a["status"], a["hits"], (a["error"] or {}).get("reason", "")[:50]],
                     "silent_difference": a["status"] < 300 and t["status"] < 300 and a["hits"] != t["hits"]}
        out["semonly"] = sm
        out["semonly_silent"] = [k for k, v in sm.items() if v["silent_difference"]]
    put(out, "EXP3-semonly-control", R)
    out["restart"] = {"counts_equal": E["after_restart"]["counts_before"] == E["after_restart"]["counts_after"], "counts": E["after_restart"]["counts_after"]}
    return out


def exp4_checks(R):
    out = {}
    for k in ("exp4_state_basic0", "exp4_state_trial", "exp4_state_trial_after_restart", "exp4_state_basic_after"):
        s = R.get(k)
        if s:
            out[k] = {"tier": s["report"]["tier"], "readback": s["readback"]["type"], "safe": s["safe"], "exact": s["exact"], "paths": s["paths"]}
    out["units"] = {n: v["tier"] for n, v in R["exp4_units"].items()}
    ent = [n for n, v in R["exp4_units"].items() if v["tier"] == "enterprise"]
    out["units_enterprise_cases"] = ent
    out["units_only_license_path"] = all(v["paths"] == ["/_license"] for v in R["exp4_units"].values())
    for k in ("exp4_fresh_start", "exp4_restart_trial", "exp4_restart_basic"):
        d = R[k]
        out[k] = {"status_sequence": d["status_sequence"], "services": {s: {kk: v[kk] for kk in ("first_tier", "first_probe_error", "reviewed_correct", "reprobe_correct", "reprobe_seconds_to_correct_after_first_200")} for s, v in d["services"].items()}}
    return out


def aggregate(d, res):
    agg = {}
    downs = [r for r in load(d, "down") if r.get("status") == "complete"]
    ups = [r for r in load(d, "up") if r.get("status") == "complete"]
    # matrix: distinct values per cell and phase across lives (a cell with more than one value is non-deterministic)
    mt = {}
    for R in downs:
        for cell, row in matrix_table(R).items():
            for ph, v in row.items():
                mt.setdefault(cell, {}).setdefault(ph, set()).add(json.dumps(v, sort_keys=True))
    agg["matrix_distinct_values_across_lives"] = {c: {ph: sorted(v) for ph, v in d2.items()} for c, d2 in mt.items()}
    agg["matrix_nondeterministic_cells"] = sorted(c for c, d2 in mt.items() if any(len(v) > 1 for v in d2.values()))
    allx = [exp2_checks(R, "down") for R in downs] + [exp2_checks(R, "up") for R in ups]
    nums = lambda key, sub=None: [v for v in ((x.get(key) if sub is None else (x.get(key) or {}).get(sub)) for x in allx) if _num(v)]
    lo = lambda xs: min(xs) if xs else None
    hi = lambda xs: max(xs) if xs else None
    ex, mm = sum(nums("T1_examined")), sum(nums("T1_mismatches"))
    agg["exp2_total_examined"], agg["exp2_total_mismatches"] = ex, mm
    agg["exp2_correct_rate_wilson_lower"] = wilson_lower(ex - mm, ex)
    agg["exp2_lives"] = {"down": len(downs), "up": len(ups)}
    agg["exp2_worst"] = {"min_examined": lo(nums("T1_examined")), "max_longest_window_s": hi(nums("T2_longest_window_s")),
                         "max_fail_fraction": hi(nums("T2_fail_fraction")), "min_control_flag_rate": lo(nums("T1_control_flag_rate")),
                         "max_flip_down_s": hi([x["T3_flip_latency_s"] for x in allx[:len(downs)] if _num(x["T3_flip_latency_s"])]),
                         "max_flip_up_s": hi([x["T3_flip_latency_s"] for x in allx[len(downs):] if _num(x["T3_flip_latency_s"])]),
                         "total_acked_writes": sum(nums("T4", "acked")), "total_acked_absent": sum(nums("T4", "acked_absent"))}
    # mandatory: at least one complete life in each direction (EXP2 has both); each life's own gate status, combined
    both = bool(downs) and bool(ups)
    agg["all_status"] = {k + "_pass": combine(x[k + "_status"] for x in allx) if both else "UNVERIFIED" for k in ("T1", "T2", "T3", "T4", "T5")}
    agg["all_pass"] = {k: v == "PASS" for k, v in agg["all_status"].items()}
    return agg


def main():
    d = sys.argv[1]
    res = {"down": [], "up": []}
    for R in load(d, "down"):
        if R.get("status") != "complete":
            res["down"].append({"file": R["_file"], "status": R.get("status"), "error": R.get("error")})
            continue
        res["down"].append({"file": R["_file"], "seed": R["seed"], "commit": R["stamp_end"]["code_commit"], "licence_end": R["licence_end"]["type"],
                            "exp1": exp1_checks(R), "exp2": exp2_checks(R, "down"), "exp3": exp3_checks(R), "exp4": exp4_checks(R),
                            "second_start_trial": R["second_start_trial"]["body"], "start_trial_first": R["start_trial"]["body"]})
    for R in load(d, "up"):
        if R.get("status") != "complete":
            res["up"].append({"file": R["_file"], "status": R.get("status"), "error": R.get("error")})
            continue
        res["up"].append({"file": R["_file"], "seed": R["seed"], "ttl": R["ttl"], "commit": R["stamp_end"]["code_commit"], "exp2": exp2_checks(R, "up"),
                          "moment": R.get("exp1_moment"), "licence_end": R["licence_end"]["type"]})
    res["aggregate"] = aggregate(d, res)
    if len(sys.argv) > 2:
        json.dump(res, open(sys.argv[2], "w"), indent=1, sort_keys=True)
    print(json.dumps(res["aggregate"], indent=1, sort_keys=True)[:6000])


if __name__ == "__main__":
    main()
