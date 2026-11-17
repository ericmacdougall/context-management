"""Threshold reapplication: applies the THRESHOLDS.md gates to the figures the harness recorded in the per-life result JSON files.
It does not recompute those figures from raw rows; recompute.py does that, and only where raw rows were saved (T2, T3, T5 for all lives;
T1 and the polling moments for down_7 only; see DEGRADE.md "Evidence status and limits"). Usage: summarize.py RESULTS_DIR [out.json]
Reads only the result files; does not import the experiment code.

Verdicts (DESIGN-VERDICTS.md): this module decides no gate. Every gate status comes from verdicts.judge(), which parses the life into typed,
immutable evidence (evidence.py: every field's domain, every cross-field relation, HTTP outcomes as a closed set of cases, rates only as
proportions of counts, controls that cannot exist without their sample); evidence that does not parse is UNVERIFIED with the parse reason,
and verdict functions see only evidence objects. The one optional field is exp2.writes.sem_failed_other (addendum D): absent, EXP2-T4 passes
only when sem_failed is 0. The per-life reports here (figures copied for reading, matrix table, exp3 operation lists, exp4) are reports,
not gates; they may raise on a malformed life file."""
import collections
import glob
import gzip
import json
import math
import os
import sys

import observe


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


def _backfill_observed(R, path):
    """Review 8 F13: lives recorded before the observation boundary (observe.py) stored refusal timings and licence-failure counts without
    the Outcome behind them. Re-derive both from the committed raw files through observe.py, and attach them only when the re-derivation
    reproduces the recorded figures (timing, successes after it, n; control n). Without the raw file, or when it does not reproduce them,
    nothing is attached and the gate is UNVERIFIED. Nothing is written to disk."""
    base = path[:-5]
    M = R.get("exp1_moment")
    if R.get("mode") == "down" and isinstance(M, dict) and isinstance(R.get("start_basic"), dict) and os.path.exists(base + ".poll.gz"):
        new = observe.moments(json.load(gzip.open(base + ".poll.gz", "rt")), R["start_basic"]["t_resp"])
        for f, m in M.items():
            n = new.get(f) or {}
            if isinstance(m, dict) and "first_refusal" not in m and n.get("first_refusal") \
                    and all(n.get(k) == m.get(k) for k in ("first_refusal_rel_resp_s", "success_after_first_refusal", "n")):
                m["first_refusal"] = n["first_refusal"]
                m["first_refusal_source"] = "re-derived from poll.gz"
    E2 = R.get("exp2")
    if not isinstance(E2, dict) or not os.path.exists(base + ".rows.gz"):
        return
    if R.get("mode") == "down":
        key, t = "T5_control_after_1s", (R.get("start_basic") or {}).get("t_resp")
        keep = lambda r: r[0] >= t + 1.0
    else:
        key, t = "T5_control_before_start_trial", (R.get("start_trial") or {}).get("t_req")
        keep = lambda r: r[1] < t
    ctl = E2.get(key)
    if not isinstance(ctl, dict) or "licence_refusals" in ctl or not isinstance(t, (int, float)):
        return
    c = observe.control_counts([json.loads(l) for l in gzip.open(base + ".rows.gz", "rt")], keep)
    if c["n"] == ctl.get("n"):   # only trust the recount when it reproduces the recorded denominator
        ctl["licence_refusals"] = c["licence_refusals"]
        ctl["licence_refusals_source"] = "recounted from rows.gz by Outcome case"


def load(d, mode):
    out = []
    for f in sorted(glob.glob(os.path.join(d, "%s_[0-9]*.json" % mode))):
        R = json.load(open(f))
        R["_file"] = os.path.basename(f)
        _backfill_up_licence_failures(R, f)
        _backfill_observed(R, f)
        out.append(R)
    return out


# ---- verdicts: every gate status comes from verdicts.judge(), which parses the life into typed evidence (evidence.py) first. Nothing in
# this module decides a gate; it writes reports and copies each verdict into them as <out>_status, <out>_pass and, when not PASS, <out>_reason.
import verdicts  # noqa: E402
from evidence import MAX_INT, MISSING  # noqa: E402


def fetch(R, path):
    """Report helper (never used by a verdict): the value at path, or MISSING."""
    v = R
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return MISSING
        v = v[k]
    return v


def _is_int(v):
    """Report helper: an integer in the measurable range (evidence.MAX_INT; a 10**400 would overflow float division, review 8 F14)."""
    return type(v) is int and abs(v) <= MAX_INT


def combine(statuses):
    """For the aggregate over lives: FAIL if any life failed; else UNVERIFIED if any is not PASS (or there are none); else PASS."""
    s = list(statuses)
    return "FAIL" if "FAIL" in s else ("UNVERIFIED" if not s or any(x != "PASS" for x in s) else "PASS")


def gate(name, R):
    return verdicts.judge(name, R).status


def put(r, name, R):
    """Write gate `name` into report dict r as <out>_status and <out>_pass (true only for PASS), with the reason when not PASS."""
    v = verdicts.judge(name, R)
    out = verdicts.GATES[name].out
    r[out + "_status"], r[out + "_pass"] = v.status, v.status == "PASS"
    if v.status != "PASS":
        r[out + "_reason"] = v.reason
    return v.status



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
    return _is_int(x) or (type(x) is float and math.isfinite(x))


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
