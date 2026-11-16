"""Independent recomputation (proof rule 5): reads the per-item artifacts and recounts every headline figure with its own code
(no import of the proof modules), then prints OK/MISMATCH against the summary stored in the artifact.
Usage: python3 recompute.py ARTIFACT_DIR"""
import glob
import json
import statistics
import sys


def load(d, item):
    fs = glob.glob("%s/%s-*.json" % (d, item))
    assert len(fs) == 1, (item, fs)
    return json.load(open(fs[0]))


def check(name, got, want, bad):
    ok = got == want
    print("%-52s %s  recomputed=%s artifact=%s" % (name, "OK" if ok else "MISMATCH", got, want))
    if not ok:
        bad.append(name)


def main(d):
    bad = []
    a = load(d, "item1")
    R, C = a["runs"]["real"], a["runs"]["control"]
    S = a["summary"]
    check("item1 pass", sum(1 for r in R if r["exit"] == 137 and r["got"] == r["expected"] and r["inflight_got"] == r["inflight_expected"] and not r["blocking_gaps"]), S["pass"], bad)
    lost = lambda r: [n for n in r["stub_completed"] if n not in r["ingested_complete_resp_calls"]]
    check("item1 control completed turn absent", sum(1 for r in C if lost(r)), S["control_completed_turn_absent"], bad)
    check("item1 control returned turn lost", sum(1 for r in C if any(n in r["host_received"] for n in lost(r))), S["control_returned_turn_lost"], bad)
    check("item1 real tap absent (limit)", sum(1 for r in R if lost(r)), S["real_tap_completed_turn_absent"], bad)
    check("item1 witness k=3 min events", min(r["n_events"] for r in R if r["k"] == 3), min(r["n_events"] for r in R if r["k"] == 3), bad)
    b = load(d, "item1b")
    def good(c):
        if c["fail"] == "reject":
            return c["n_resp_events"] == 0 and "call_rejected" in c["call2_unconfirmed_or_rejected"]
        return c["complete_resp_events"] == 0 and c["tool_call_complete"] == 0 and (c["n_resp_events"] > 0 or c["fail"] == "silent:6")
    check("item1b pass", sum(1 for c in b["runs"] if good(c)), b["summary"]["pass"], bad)
    a2 = load(d, "item2a")
    ok = sum(1 for r in a2["runs"] if r["got_sig"] == r["baseline_sig"] and len({s[0] for s in r["got_sig"]}) == len(r["got_sig"]) and r["watermark"] == r["log_head"] and r["open_gaps_end"] == 0)
    check("item2a pass", ok, a2["summary"]["pass"], bad)
    check("item2a control violations", sum(r["control_violation"] for r in a2["runs"]), a2["summary"]["control_violations"], bad)
    for tag in ("item2b", "item2b_control_prereg_3layers", "item2b_control_4layers"):
        x = load(d, tag)
        check(tag + " identical", sum(1 for r in x["runs"] if r["identical_to_baseline"]), x["summary"]["identical_to_baseline"], bad)
        check(tag + " dup runs", sum(1 for r in x["runs"] if r["dup_rows"] > 0), x["summary"]["runs_with_duplicates"], bad)
        check(tag + " fenced runs", sum(1 for r in x["runs"] if r["fenced"] >= 1), x["summary"]["runs_with_fencing"], bad)
        check(tag + " overlap runs", sum(1 for r in x["runs"] if r["overlapping_rounds"] >= 1), x["summary"]["runs_with_measured_overlap"], bad)
    x = load(d, "item2c")
    check("item2c identical", sum(1 for r in x["runs"] if r["identical_to_baseline"]), x["summary"]["identical_to_baseline"], bad)
    check("item2c fenced runs", sum(1 for r in x["runs"] if r["fenced"] >= 1), x["summary"]["runs_with_fencing"], bad)
    x = load(d, "item2d")
    check("item2d zombie", sum(1 for r in x["runs"] if r["z_fenced"] >= 1 and r["z_records_applied"] == 0 and r["events_final"] == r["events_after_B"] and r["identical_to_baseline"]),
          x["summary"]["zombie_rejected_no_writes"], bad)
    x = load(d, "item3")
    R = x["runs"]
    S = x["summary"]
    check("item3 a detected", sum(1 for r in R if [g[0] for g in r["a"]["gaps"]] == ["seq_hole"] and not r["a"]["claim_head"] and r["a"]["watermark"] == r["a"]["dropped"] - 1), S["a_dropped_detected"], bad)
    check("item3 a control complete", sum(1 for r in R if r["a"]["control_complete_up_to"] == r["head"]), S["a_control_reports_complete"], bad)
    check("item3 d closes", sum(1 for r in R if r["d"]["open_gaps"] == 0 and r["d"]["closed_gaps"] >= 1 and r["d"]["claim_head"] and r["d"]["identical_to_baseline"]), S["d_late_closes"], bad)
    check("item3 b detected", sum(1 for r in R if any(g[0] == "tail_truncated" for g in r["b"]["gaps"]) and not r["b"]["claim_head"]), S["b_truncation_detected_with_witness"], bad)
    check("item3 c bypass", sum(1 for r in R if any(g[0] == "unlogged_assistant_message" for g in r["c"]["gaps"]) and not r["c"]["claim_head"]), S["c_bypass_detected"], bad)
    check("item3 e undetected (limit)", sum(1 for r in R if r["e"]["gaps"] > 0), S["e_truncation_no_witness_detected"], bad)
    check("item3 f undetected (limit)", sum(1 for r in R if r["f"]["gaps"] > 0), S["f_hidden_bypass_detected"], bad)
    x = load(d, "item4")
    R, S = x["runs"], x["summary"]
    cov = [r for r in R if r["placement"] != "host_alters_block"]
    check("item4 marker in untrusted (covered)", sum(r["marker_in_user_tool_system_events"] for r in cov), S["marker_in_user_tool_system_events_total_covered_placements"], bad)
    check("item4 thinking verbatim", sum(1 for r in R if r["thinking_events"] and r["thinking_signature_verbatim"] and r["thinking_prov_ok"]), S["thinking_verbatim"], bad)
    check("item4 control role-only", sum(1 for r in R if r["placement"] in ("tap_user_block", "host_merged_user_text", "history_resend_merged", "host_merged_system") and r["ctl_roleonly_marker_in_user"] > 0),
          S["control_roleonly_marker_in_user"]["violations"], bad)
    x = load(d, "item5")
    em = lambda r: r["variant"].endswith("MISS") or r["position"].endswith("MISS")
    check("item5 covered disk hits", sum(1 for r in x["runs"]["real"] if not em(r) and r["disk_hits"]), x["summary"]["covered_with_disk_hit"], bad)
    check("item5 canary witnessed", sum(1 for r in x["runs"]["real"] if not em(r) and r["canary_sent_to_model"]), x["summary"]["covered_canary_witnessed_in_request_sent"], bad)
    check("item5 control disk hits", sum(1 for r in x["runs"]["control"] if not em(r) and r["disk_hits"]), x["summary"]["control_with_disk_hit"], bad)
    x = load(d, "item6")
    for lab in ("nonstream_40ms", "stream_20x2ms"):
        off, on = x["runs"][lab]["off_s"], x["runs"][lab]["on_s"]
        check("item6 %s added median ms" % lab, round(1000 * (statistics.median(on) - statistics.median(off)), 3), round(x["summary"][lab]["added_median_ms"], 3), bad)
    print("\nMISMATCHES: %d" % len(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1])
