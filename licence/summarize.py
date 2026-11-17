"""Independent recomputation of every verdict from the per-life result JSON files (THRESHOLDS.md). Usage: summarize.py RESULTS_DIR [out.json]
Reads only the result files; does not import the experiment code."""
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


def exp1_checks(R):
    r = {}
    m = R.get("exp1_moment", {})
    r["T2_first_refusal_s"] = {f: v.get("first_refusal_rel_resp_s") for f, v in m.items()}
    r["T2_pass"] = all(v.get("first_refusal_rel_resp_s") is not None and v["first_refusal_rel_resp_s"] <= 5.0 and v.get("success_after_first_refusal") == 0 for v in m.values() if not v.get("skipped"))
    r["T2_dls_returned_before"] = m.get("dls_search", {}).get("dls_returned_counts_before")
    after = R["exp1_after"]
    latch_cells = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
    r["T3_latch_shape_ok"] = {c: (after[c].get("status") == 403 and "current license is non-compliant" in json.dumps(after[c].get("error")).lower()) for c in latch_cells}
    d = after.get("dls_search_as_user", {})
    r["T4_dls_after"] = {"status": d.get("status"), "class": d.get("class"), "returned": d.get("returned"), "reason": (d.get("error") or {}).get("reason", "")[:90]}
    r["T4_pass"] = d.get("status") in (401, 403) and d.get("returned") in (None, 0) and d.get("class") != "UNVERIFIED_PREREQUISITE"   # a 401 for a missing user is not DLS
    r["T4_control_before_returned"] = r["T2_dls_returned_before"]
    if "dls_battery_after" in R:
        a, b = R["dls_battery_after"], R["dls_battery_trial"]
        r["T4_battery_after_statuses"] = {k: v["status"] for k, v in a.items()}
        r["T4_battery_trial_statuses"] = {k: v["status"] for k, v in b.items()}
        r["T4_battery_pass"] = all(v["status"] >= 400 and not v["returned_hidden_doc"] and not v["returned_any_source"] for v in a.values())
        r["T4_battery_control_trial_hidden_leak"] = [k for k, v in b.items() if v["returned_hidden_doc"]]
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


def exp2_checks(R, direction):
    e = R["exp2"]
    r = {"T1_examined": e["T1_examined"], "T1_mismatches": e["T1_mismatches"], "T1_control_flag_rate": e["T1_control_broken_fallback"]["flag_rate"],
         "T1_control_n": e["T1_control_broken_fallback"]["client_labelled"], "T1_control_backend_wrong": e["T1_control_broken_fallback"]["backend_wrong"],
         "T2_fail_fraction": e["T2_failed_requests"] / max(1, e["T2_non_control_requests"]), "T2_longest_window_s": e["T2_longest_failure_window_s"],
         "T3_flip_latency_s": e["T3_flip_latency_s"], "ttl": R["ttl"], "T6_rrf": e.get("T6_rrf_backend_vs_client_top10"), "T6_linear": e.get("T6_linear_backend_vs_client_rrf_top10")}
    w = e["writes"]
    r["T4"] = {"acked": w["acked"], "acked_absent": w["acked_absent"], "present_unacked": w["present_unacked"], "failed_batches": w["failed_batches"],
               "sem_acked": w["sem_acked"], "sem_acked_absent": w["sem_acked_absent"], "sem_failed": w["sem_failed"], "sem_failed_loud": w["sem_failed_loud_licence_or_missing"],
               "sem_failed_present": w["sem_failed_present"], "control_detects_injected_loss": w["control_injected_loss_detected"]}
    base = (w["acked_absent"] == 0 and w["sem_acked_absent"] == 0 and w["sem_failed_present"] == 0 and w["control_injected_loss_detected"])
    # legacy rule (addendum C predecessor): every semantic failure was counted loud if it carried any 403/404 error body
    r["T4_legacy_pass"] = base and w["sem_failed"] == w["sem_failed_loud_licence_or_missing"]
    if "sem_failed_other" in w:
        r["T4_status"] = "PASS" if base and w["sem_failed_other"] == 0 and w["sem_failed"] == w["sem_failed_loud_licence_or_missing"] else "FAIL"
    elif w["sem_failed"] == 0:
        r["T4_status"] = "PASS" if base else "FAIL"
    else:   # failures exist but were never classified under addendum C and the rows were not saved (addendum D)
        r["T4_status"] = "UNVERIFIED" if base else "FAIL"
    r["T4_pass"] = r["T4_status"] == "PASS"
    if direction == "down":
        c = e["T5_control_after_1s"]
        r["T5"] = {"n": c["n"], "fail_fraction": c["fail_fraction"], "licence_failures": c["licence_failures"]}
        r["T3_pass"] = e["T3_flip_latency_s"] is not None and e["T3_flip_latency_s"] <= 5.0 and e.get("T3_backend_after_first_client") == 0
        r["T5_pass"] = c["n"] > 0 and c.get("licence_failures") is not None and c["licence_failures"] / c["n"] >= 0.95
    else:
        pre = e["T5_control_before_start_trial"]
        r["T5"] = {"before_n": pre["n"], "before_failures": pre["failures"], "before_licence_failures": pre.get("licence_failures"), "after_trial_n": e["T5_control_after_trial"]["n"], "after_trial_success": e["T5_control_after_trial"]["success"]}
        r["T3_pass"] = (e["T3_flip_latency_s"] is not None and e["T3_flip_latency_s"] <= R["ttl"] + 5.0 and e.get("T3_client_after_first_backend") == 0)
        post = e["T5_control_after_trial"]
        r["T5_pass"] = (pre["n"] > 0 and pre.get("licence_failures") is not None and pre["licence_failures"] / pre["n"] >= 0.95
                        and post["n"] > 0 and post["success"] == post["n"])
        r["sem_first_ok_after_trial_s"] = e.get("sem_first_ok_after_trial_s")
    r["T1_backend_without_oracle"] = e.get("T1_backend_without_oracle")
    r["T1_pass"] = e.get("T1_backend_without_oracle") == 0 and e["T1_mismatches"] == 0 and e["T1_examined"] >= 20000 and r["T1_control_flag_rate"] >= 0.9 and r["T1_control_backend_wrong"] == 0
    r["T2_pass"] = r["T2_fail_fraction"] <= 0.001 and r["T2_longest_window_s"] <= 2.0
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
    M = E["migration"]
    chk = {}
    if "error" in M:
        chk["error"] = M["error"]
    else:
        b = M["bbq_disk_to_hnsw"]
        chk["bbq_disk_to_hnsw"] = b["dst_count"] == b["src_count"] and b["vector_mismatches"] == 0 and b["knn_self_top1"] / b["knn_self_of"] >= 0.99 and b["src_vs_ground_truth_max_abs"] <= 1e-6
        b2 = M["bbq_disk_to_bbq_hnsw"]
        chk["bbq_disk_to_bbq_hnsw"] = b2["dst_count"] == b2["src_count"] and b2["vector_mismatches"] == 0 and b2["knn_self_top1"] / b2["knn_self_of"] >= 0.99
        s = M["semantic_to_text_vector"]
        chk["semantic_to_text_vector"] = (s["dst_count"] == s["docs"] and s["vector_mismatches_vs_stored"] == 0 and s["worst_abs_err_vs_stub"] <= 0.005 and s["worst_cosine_vs_stub"] >= 0.9999
                                          and s["knn_self_top1"] / s["knn_self_of"] >= 0.99 and s["skipped_not_single_chunk"] == 0)
        chk["control_plain"] = M["control_plain_to_hnsw"]["vector_mismatches"] == 0 and M["control_plain_to_hnsw"]["dst_count"] == M["control_plain_to_hnsw"]["src_count"]
        chk["control_corrupt_caught"] = M["control_corrupted_vs_true_source"]["vector_mismatches"] == 1
        # independent source counts: `_count` of the source index recorded after the operations and before the restart (never derived from the scroll)
        ind = E["after_restart"]["counts_before"]
        chk["source_count_independent"] = (b["src_count"] == ind["bbq_disk"] and b2["src_count"] == ind["bbq_disk"] and s["docs"] + s["skipped_not_single_chunk"] == ind["sem"]
                                           and M["control_plain_to_hnsw"]["src_count"] == ind["plain"])
        for k in ("bbq_disk_to_hnsw", "bbq_disk_to_bbq_hnsw", "semantic_to_text_vector"):
            chk[k] = chk[k] and chk["source_count_independent"]
        chk["migrated_ops_ok"] = all(v < 300 for d in M["migrated_basic_ops"].values() for v in d.values())
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
        out["semonly_trial_witness_ok"] = all(R["semonly_trial"][k]["hits"] and R["semonly_trial"][k]["hits"] > 0 for k in ("semantic", "match", "match_all"))
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
    ex = sum(x["T1_examined"] for x in allx)
    mm = sum(x["T1_mismatches"] for x in allx)
    agg["exp2_total_examined"], agg["exp2_total_mismatches"] = ex, mm
    agg["exp2_correct_rate_wilson_lower"] = wilson_lower(ex - mm, ex)
    agg["exp2_lives"] = {"down": len(downs), "up": len(ups)}
    agg["exp2_worst"] = {"min_examined": min(x["T1_examined"] for x in allx), "max_longest_window_s": max(x["T2_longest_window_s"] for x in allx),
                         "max_fail_fraction": max(x["T2_fail_fraction"] for x in allx), "min_control_flag_rate": min(x["T1_control_flag_rate"] for x in allx),
                         "max_flip_down_s": max((exp2_checks(R, "down")["T3_flip_latency_s"] for R in downs), default=None),
                         "max_flip_up_s": max((exp2_checks(R, "up")["T3_flip_latency_s"] for R in ups), default=None),
                         "total_acked_writes": sum(x["T4"]["acked"] for x in allx), "total_acked_absent": sum(x["T4"]["acked_absent"] for x in allx)}
    agg["all_pass"] = {k: all(x[k] for x in allx) for k in ("T1_pass", "T2_pass", "T3_pass", "T4_pass", "T5_pass")}
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
