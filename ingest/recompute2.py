"""Independent recomputation for the follow-up artifacts (PREREG-amend-2) and the OpenAI reruns. No import of the proof modules.
Usage: python3 recompute2.py ARTIFACT_DIR   (recompute.py covers the base Anthropic items)"""
import glob
import json
import math
import statistics
import sys


def load(d, item):
    fs = glob.glob("%s/%s-*.json" % (d, item))
    assert len(fs) == 1, (item, fs)
    return json.load(open(fs[0]))


def check(name, got, want, bad):
    ok = got == want
    print("%-58s %s  recomputed=%s artifact=%s" % (name, "OK" if ok else "MISMATCH", got, want))
    if not ok:
        bad.append(name)


def main(d):
    bad = []
    a = load(d, "item1")
    check("item1 restated witness cells", sum(1 for r in a["runs"]["real"] if r["stub_received_calls"] == r["expected_received_calls"] and r["log_records"] == r["expected_log_records"]),
          a["summary"]["restated_witness_cells_ok"], bad)
    lost = lambda r: [n for n in r["stub_completed"] if n not in r["ingested_complete_resp_calls"]]
    check("item1 control completed turn absent", sum(1 for r in a["runs"]["control"] if lost(r)), a["summary"]["control_completed_turn_absent"], bad)
    x = load(d, "item2e")
    rows = x["runs"]
    for r in rows:
        r["dups"] = sum(1 for p in r["per_run"] if p["dup_rows"] > 0)
        r["ident"] = sum(1 for p in r["per_run"] if p["identical"])
    check("item2e single-layer sufficient", sorted(r["config"] for r in rows if r["config"].startswith("only_") and r["ident"] == r["runs"] and r["dups"] == 0),
          sorted(x["summary"]["single_layer_sufficient"]), bad)
    check("item2e all_off duplicate runs", [r["dups"] for r in rows if r["config"] == "all_off"][0], x["summary"]["registered_control_all_off_duplicate_runs"], bad)
    for sfx in ("_openai",):
        y = load(d, "item2a" + sfx)
        check("item2a" + sfx + " pass", sum(1 for r in y["runs"] if r["got_sig"] == r["baseline_sig"] and len({s[0] for s in r["got_sig"]}) == len(r["got_sig"]) and r["watermark"] == r["log_head"] and r["open_gaps_end"] == 0), y["summary"]["pass"], bad)
        check("item2a" + sfx + " control violations", sum(r["control_violation"] for r in y["runs"]), y["summary"]["control_violations"], bad)
        y = load(d, "item2b" + sfx)
        check("item2b" + sfx + " identical", sum(1 for r in y["runs"] if r["identical_to_baseline"]), y["summary"]["identical_to_baseline"], bad)
        check("item2b" + sfx + " overlap runs", sum(1 for r in y["runs"] if r["overlapping_rounds"] >= 1), y["summary"]["runs_with_measured_overlap"], bad)
        y = load(d, "item2c" + sfx)
        check("item2c" + sfx + " identical", sum(1 for r in y["runs"] if r["identical_to_baseline"]), y["summary"]["identical_to_baseline"], bad)
        y = load(d, "item2d" + sfx)
        check("item2d" + sfx + " zombie", sum(1 for r in y["runs"] if r["z_fenced"] >= 1 and r["z_records_applied"] == 0 and r["events_final"] == r["events_after_B"] and r["identical_to_baseline"]),
              y["summary"]["zombie_rejected_no_writes"], bad)
    for tag in ("item3", "item3_openai"):
        x = load(d, tag)
        R, S = x["runs"], x["summary"]
        check(tag + " a detected", sum(1 for r in R if [g[0] for g in r["a"]["gaps"]] == ["seq_hole"] and not r["a"]["claim_head"] and r["a"]["watermark"] == r["a"]["dropped"] - 1), S["a_dropped_detected"], bad)
        check(tag + " c bypass", sum(1 for r in R if any(g[0] == "unlogged_assistant_message" for g in r["c"]["gaps"]) and not r["c"]["claim_head"]), S["c_bypass_detected"], bad)
        check(tag + " e false complete claims", sum(1 for r in R if r["e"]["claim_cut"]), S["e_false_complete_claims"], bad)
        check(tag + " f undetected", sum(1 for r in R if r["f"]["gaps"] > 0), S["f_hidden_bypass_detected"], bad)
    for tag in ("item4", "item4_openai"):
        x = load(d, tag)
        R, S = x["runs"], x["summary"]
        cov = [r for r in R if r["placement"] != "host_alters_block"]
        check(tag + " marker in untrusted", sum(r["marker_in_user_tool_system_events"] for r in cov), S["marker_in_user_tool_system_events_total_covered_placements"], bad)
        check(tag + " quote back flagged", sum(1 for r in R if r["placement"] == "model_quotes_back" and r["assistant_quote_events"] >= 1 and r["assistant_quote_flagged"] == r["assistant_quote_events"]), S["quote_back_stays_assistant_and_flagged"], bad)
        check(tag + " forged stays user", sum(1 for r in R if r["placement"] == "user_forges_block" and r["forged_in_user_events"] >= 1 and r["injected_events"] == 0), S["forged_stays_user"], bad)
    em = lambda r: r["variant"].endswith("MISS") or r["position"].endswith("MISS")
    for tag in ("item5", "item5_openai"):
        x = load(d, tag)
        check(tag + " covered disk hits", sum(1 for r in x["runs"]["real"] if not em(r) and r["disk_hits"]), x["summary"]["covered_with_disk_hit"], bad)
        check(tag + " control disk hits", sum(1 for r in x["runs"]["control"] if not em(r) and r["disk_hits"]), x["summary"]["control_with_disk_hit"], bad)
        check(tag + " canary witnessed", sum(1 for r in x["runs"]["real"] if not em(r) and r["canary_sent_to_model"]), x["summary"]["covered_canary_witnessed_in_request_sent"], bad)
    x = load(d, "item6_openai")
    for lab in ("nonstream_40ms", "stream_20x2ms"):
        off, on = x["runs"][lab]["off_s"], x["runs"][lab]["on_s"]
        check("item6_openai %s added median ms" % lab, round(1000 * (statistics.median(on) - statistics.median(off)), 3), round(x["summary"][lab]["added_median_ms"], 3), bad)
    x = load(d, "item8")
    C = x["runs"]["crash"]
    check("item8 live loses deltas", sum(1 for r in C if r["delivery"] == "live" and r["exit"] == 137 and r["host_events_received"] >= 1 and r["response_events_in_memory"] == 0), x["summary"]["live_host_saw_deltas_memory_has_none"], bad)
    check("item8 buffered gates", sum(1 for r in C if r["delivery"] == "buffered" and r["exit"] == 137 and r["host_events_received"] == 0 and r["response_events_in_memory"] == 0), x["summary"]["buffered_host_saw_nothing"], bad)
    x = load(d, "item9")
    for k, t in x["runs"].items():
        m = statistics.median
        check("item9 %s ratio" % k, round(m(t[180:]) / m(t[10:30]), 4), round(x["summary"][k]["ratio_181_200_over_11_30"], 4), bad)
    x = load(d, "item10")
    R, S = x["runs"], x["summary"]
    check("item10 states", sum(1 for r in R["states"] if r["state"] == "complete_except_unconfirmed" and r["unconfirmed"] == [1] and not r["claim_complete"] and r["claim_except"][0]), S["states_ok"], bad)
    check("item10 recover", sum(1 for r in R["recover"] if r["recovered"] == 1 and r["state"] == "complete" and r["provs"] == ["user", "assistant"]), S["recover_ok"], bad)
    check("item10 seed good", sum(1 for r in R["seed"] if r["good"]["gap_kinds"] == [] and r["good"]["old_history_events"] == 0 and r["good"]["chat_events"] == 2 and r["good"]["state"] == "complete"), S["seed_good_ok"], bad)
    check("item10 seed bad", sum(1 for r in R["seed"] if "seed_invalid" in r["bad"]["gap_kinds"] and r["bad"]["state"] == "incomplete"), S["seed_bad_gap"], bad)
    check("item10 backfill authorised", sum(1 for r in R["backfill"] if r["authorised"]["detected"] == 1 and r["authorised"]["open_blocking_after"] == 0 and r["authorised"]["closed_by"] == ["backfill:ops:alice"]), S["backfill_authorised_closes"], bad)
    check("item10 backfill unauthorised closes", sum(1 for r in R["backfill"] if r["unauthorised"]["open_blocking_after"] == 0), S["backfill_unauthorised_closes"], bad)
    check("item10 storage", sum(1 for r in R["storage"] if "issued" not in r["tables"] and r["host_events"] == 0 and r["gaps"] == 0 and r["events_with_block_text"] == 0 and r["blob_update"] == "refused"), S["storage_ok"], bad)
    x = load(d, "item11")
    ok = sum(1 for r in x["runs"] if r["user_events"] == r["calls"] == r["distinct_user"] and r["assistant_events"] == r["calls"] == r["distinct_assistant"]
             and not r["blocking_gaps"] and r["summary_provs"] in ([], ["host"]) and (r["host"] != "compacting" or r["summary_events"] > 0))
    check("item11 pass", ok, x["summary"]["pass"], bad)
    x = load(d, "item12")
    for k, rk, sk in (("heldout", "heldout_rows", "heldout"), ("tuned", "tuned_rows", "tuned_for_comparison")):
        rows = x["runs"][rk]
        S = x["summary"][sk]
        check("item12 %s whole_string_absent (old predicate)" % k, [sum(r["whole_string_absent"] for r in rows), len(rows)], [S["overall_whole_string_absent"], S["overall_n"]], bad)
        check("item12 %s complete_redaction (corrected)" % k, [sum(r["complete_redaction"] for r in rows), len(rows)], [S["overall_complete_redaction"], S["overall_n"]], bad)
        check("item12 %s detected" % k, sum(r["detected"] for r in rows), S["overall_detected"], bad)
        # per-class: recomputed from rows
        per = {}
        for r in rows:
            q = per.setdefault(r["variant"], [0, 0])
            q[0] += r["complete_redaction"]
            q[1] += r["whole_string_absent"]
        check("item12 %s per-class" % k, {v: q for v, q in per.items()}, {v: [p["complete_redaction"], p["whole_string_absent"]] for v, p in S["per_variant"].items()}, bad)
    x = load(d, "item12b")
    check("item12b benign changed", [sum(r["changed"] for r in x["runs"]), len(x["runs"])], [x["summary"]["overall_changed"], x["summary"]["overall_n"]], bad)
    for tag in ("item2a", "item2b", "item2c", "item2d", "item2a_openai", "item2b_openai", "item2c_openai", "item2d_openai"):
        y = load(d, tag)
        check(tag + " oracle matches", sum(1 for r in y["runs"] if r["oracle_match"]), y["summary"]["oracle_identical"], bad)
    y = load(d, "item2b_control_4layers")
    check("item2b_control_4layers all four layers off in header", [y["header"]["params"][k] for k in ("fence", "unique", "state_guard", "chain")], [False] * 4, bad)
    check("item2b_control_4layers duplicate runs (floor >= 5)", sum(1 for r in y["runs"] if r["dup_rows"] > 0), y["summary"]["runs_with_duplicates"], bad)
    y = load(d, "item2f")
    check("item2f arm equals baseline", sum(r["arm_equals_baseline"] for r in y["runs"]), y["summary"]["arm_equals_baseline"], bad)
    check("item2f oracle mismatch", sum(1 for r in y["runs"] if not r["oracle_match"]), y["summary"]["oracle_mismatch"], bad)
    y = load(d, "item13")
    rep = [r for r in y["runs"] if r["pattern"] != "unique"]
    uni = [r for r in y["runs"] if r["pattern"] == "unique"]
    check("item13 repeated pass", sum(1 for r in rep if r["user_events"] == 20 and r["assistant_events"] == 20 and r["oracle_match"] and not r["blocking_gaps"]), y["summary"]["repeated_pass"], bad)
    check("item13 repeated runs with ambiguity flag", sum(1 for r in rep if r["ambiguous_calls"]), y["summary"]["repeated_runs_with_ambiguity_flag"], bad)
    check("item13 unique ambiguity flags", sum(1 for r in uni if r["ambiguous_calls"]), y["summary"]["unique_runs_with_ambiguity_flag"], bad)
    y = load(d, "item13b")
    R = y["runs"]
    check("item13b undeclared", sum(1 for r in R if r["undeclared"]["user_events"] == 2 and r["undeclared"]["flagged_user_events"] == 1 and r["undeclared"]["ambiguous_calls"] == [2]), y["summary"]["undeclared_flagged_duplicate"], bad)
    check("item13b declared", sum(1 for r in R if r["declared"]["user_events"] == 1 and r["declared"]["assistant_events"] == 2 and "declared_retry" in r["declared"]["port_kinds"] and r["declared"]["ambiguous_calls"] == []), y["summary"]["declared_suppressed_visibly"], bad)
    check("item13b false claim", sum(1 for r in R if r["false_claim"]["user_events"] == 2 and "retry_claim_ignored" in r["false_claim"]["port_kinds"] and "declared_retry" not in r["false_claim"]["port_kinds"]), y["summary"]["false_claim_ignored"], bad)
    y = load(d, "item11")
    check("item11 ambiguity flags (unique text)", sum(1 for r in y["runs"] if r["ambiguous_calls"]), y["summary"]["runs_with_ambiguity_flag"], bad)
    print("\nMISMATCHES: %d" % len(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1])
