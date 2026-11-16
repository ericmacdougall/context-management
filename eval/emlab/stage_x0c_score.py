"""Score STAGE-X0C (stability and preference-line check, 40 calls; no model call, no judge). Rules: STAGE-X0C.md (committed before the calls).
Arm S = identical prompt extract-v2-cap30; arm P = extract-v3-pref. Twenty sessions: 10 with a prior cap-30 output (Stage X0B) give the identical-prompt
replicate pairs; the other 10 give a first cap-30 output only. Failures stay in the denominators. Twenty sessions give a direction, not a rate."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from . import provenance_x04 as prov
from .stage_x0_score import dist, mean
from .stage_x0b_score import quote_match

GATE_STABILITY = 0.85
GATE_WORST_SESSION = 0.60


def matched_into(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> int:
    return sum(1 for x in a if any(quote_match(x.get("quote", ""), y.get("quote", "")) for y in b))


def pair_stability(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Both directions: stability = (facts of A found in B + facts of B found in A) / (|A| + |B|)."""
    fa, fb = matched_into(a, b), matched_into(b, a)
    n = len(a) + len(b)
    return {"a": len(a), "b": len(b), "a_in_b": fa, "b_in_a": fb, "stability": (fa + fb) / float(n) if n else None}


def cluster_wilson_mean(rates: List[float]) -> Dict[str, float]:
    """Wilson interval for the mean of per-session rates with n = number of sessions (real-valued successes)."""
    n = len(rates)
    if not n:
        return {"n": 0, "mean": None, "low": None, "high": None}
    p = sum(rates) / n
    lo, hi = prov.wilson(p * n, n)
    return {"n": n, "mean": p, "low": lo, "high": hi}


def user_stats(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    u = sum(1 for f in facts if f.get("author") == "user")
    return {"total": len(facts), "user": u, "user_share": (u / float(len(facts))) if facts else None}


def main(out_root, in_root, out_json):
    key = json.load(open(os.path.join(out_root, "stage_x0c_key", "selection_key.json")))
    rows = {r["opaque_session_id"]: r for r in key["sessions"]}
    sess_files = {o: json.load(open(os.path.join(out_root, "stage_x0c", "sessions", o + ".json"), encoding="utf-8")) for o in rows}

    def load(arm_dir, o):
        p = os.path.join(in_root, "stage_x0c", arm_dir, o + ".json")
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"status": "missing", "facts": []}
    S = {o: load("S", o) for o in rows}
    P = {o: load("P", o) for o in rows}
    prev30 = {o: json.load(open(os.path.join(in_root, "stage_x0b", o + ".json"), encoding="utf-8")) for o in rows if rows[o]["has_prior_cap30"]}
    old12 = {o: json.load(open(os.path.join(in_root, "stage_x0", o + ".json"), encoding="utf-8")) for o in rows}
    ok = lambda r: r.get("status") in ("ok", "repaired")
    res: Dict[str, Any] = {"pilot": True, "confirmatory": False, "n_sessions": len(rows), "direction_not_rate": True,
                           "status_counts": {"S": {s: sum(1 for r in S.values() if r.get("status") == s) for s in {r.get("status") for r in S.values()}},
                                             "P": {s: sum(1 for r in P.values() if r.get("status") == s) for s in {r.get("status") for r in P.values()}}}}
    # ---- stability: identical prompt, two cap-30 runs (X0B output against arm S), the 10 replicate sessions
    pairs = {}
    for o in prev30:
        if ok(S[o]) and ok(prev30[o]):
            pairs[o] = pair_stability(prev30[o]["facts"], S[o]["facts"])
    rates = [v["stability"] for v in pairs.values() if v["stability"] is not None]
    tot_n = sum(v["a"] + v["b"] for v in pairs.values())
    tot_m = sum(v["a_in_b"] + v["b_in_a"] for v in pairs.values())
    pooled = (tot_m / float(tot_n)) if tot_n else None
    res["stability_identical_prompt"] = {"pairs": pairs, "n_pairs": len(pairs), "pooled_stability": pooled, "session_mean": mean(rates), "worst_session": min(rates) if rates else None,
                                         "wilson_by_session": cluster_wilson_mean(rates), "gate": GATE_STABILITY, "gate_worst_session": GATE_WORST_SESSION,
                                         "gate_met": bool(pooled is not None and pooled >= GATE_STABILITY and rates and min(rates) >= GATE_WORST_SESSION),
                                         "missing_or_failed_pairs": len(prev30) - len(pairs),
                                         "note": "the 10 replicate pairs are X0B (run 1) against arm S (run 2); both are extract-v2-cap30; 10 sessions give a direction, not a rate"}
    # arm S on the 10 sessions without a prior cap-30 run: first cap-30 output against the cap-12 output (cap change and sampling confounded)
    first = {o: pair_stability(old12[o]["facts"], S[o]["facts"]) for o in rows if o not in prev30 and ok(S[o])}
    res["first_cap30_vs_cap12_no_replicate"] = {"sessions": len(first), "pooled": (sum(v["a_in_b"] + v["b_in_a"] for v in first.values()) / float(max(1, sum(v["a"] + v["b"] for v in first.values())))) if first else None,
                                                "mean_facts_cap12": mean(len(old12[o]["facts"]) for o in first), "mean_facts_cap30": mean(len(S[o]["facts"]) for o in first),
                                                "note": "NOT an identical-prompt comparison: the cap changed from 12 to 30 between the two runs"}
    # ---- arm P against arm S on the same session
    per: Dict[str, Any] = {}
    for o in rows:
        if not (ok(S[o]) and ok(P[o])):
            per[o] = {"status_S": S[o].get("status"), "status_P": P[o].get("status")}
            continue
        sf, pf = S[o]["facts"], P[o]["facts"]
        per[o] = {"S": user_stats(sf), "P": user_stats(pf), "S_facts_displaced_in_P": len(sf) - matched_into(sf, pf), "P_facts_new_vs_S": len(pf) - matched_into(pf, sf),
                  "P_at_cap_30": len(pf) >= 30, "types": rows[o]["question_types"], "group": rows[o]["group"]}
    both = [o for o in per if "S" in per[o]]
    sU = sum(per[o]["S"]["user"] for o in both)
    pU = sum(per[o]["P"]["user"] for o in both)
    sT = sum(per[o]["S"]["total"] for o in both)
    pT = sum(per[o]["P"]["total"] for o in both)
    disp = sum(per[o]["S_facts_displaced_in_P"] for o in both)
    # excess displacement on the replicate sessions: P-vs-S displacement minus the identical-prompt non-reproduction of the same facts (S vs X0B)
    rep_disp = {o: {"S_displaced_in_P": per[o]["S_facts_displaced_in_P"], "S_n": per[o]["S"]["total"],
                    "S_not_reproduced_vs_prev": len(S[o]["facts"]) - matched_into(S[o]["facts"], prev30[o]["facts"])} for o in both if o in prev30}
    res["arm_P_vs_S"] = {"per_session": per, "sessions_compared": len(both), "user_facts_S": sU, "user_facts_P": pU, "mean_user_facts_S": sU / float(len(both)) if both else None,
                         "mean_user_facts_P": pU / float(len(both)) if both else None, "mean_user_increase": (pU - sU) / float(len(both)) if both else None,
                         "sessions_with_more_user_facts_in_P": sum(1 for o in both if per[o]["P"]["user"] > per[o]["S"]["user"]),
                         "user_share_S": (sU / float(sT)) if sT else None, "user_share_P": (pU / float(pT)) if pT else None, "total_facts_S": sT, "total_facts_P": pT,
                         "P_at_cap_30": sum(1 for o in both if per[o]["P_at_cap_30"]), "S_facts_displaced_in_P_total": disp, "S_facts_displaced_share": (disp / float(sT)) if sT else None,
                         "replicate_sessions_displacement": rep_disp,
                         "excess_displacement_share_on_replicate_sessions": ((sum(v["S_displaced_in_P"] for v in rep_disp.values()) - sum(v["S_not_reproduced_vs_prev"] for v in rep_disp.values())) / float(max(1, sum(v["S_n"] for v in rep_disp.values())))) if rep_disp else None}
    # ---- quote and speaker check on arm P facts (and arm S for comparison)
    sess = {o: prov.rounds_of_turns(sess_files[o]["turns"]) for o in sess_files}

    def prov_arm(R):
        facts = [dict(f, session_id=o) for o in rows if ok(R[o]) for f in (R[o].get("facts") or [])]
        r = prov.check_all(facts, sess) if facts else []
        a_pass = sum(1 for x in r if x["a"] == prov.PASS)
        lo, hi = prov.wilson(a_pass, len(r)) if r else (0.0, 0.0)
        users = [(f, x) for f, x in zip(facts, r) if f.get("author") == "user"]
        u_a = [x for f, x in users if x["a"] == prov.PASS]
        return {"facts": len(r), "a_pass": a_pass, "a_rate": (a_pass / len(r)) if r else None, "wilson_low": lo, "wilson_high": hi,
                "b": {k: sum(1 for x in r if x["b"] == k) for k in ("pass", "ambiguous", "mismatch", "undefined")},
                "user_authored_facts": len(users), "user_a_pass": len(u_a), "user_speaker_pass_among_a": sum(1 for x in u_a if x["b"] == "pass"),
                "user_speaker_mismatch": sum(1 for x in u_a if x["b"] == "mismatch"), "user_speaker_ambiguous": sum(1 for x in u_a if x["b"] == "ambiguous"),
                "controls": prov.controls(facts, sess, r) if r else None}
    res["provenance"] = {"arm_S": prov_arm(S), "arm_P": prov_arm(P)}
    # ---- per call: tokens, seconds, cost, arm P against arm S on the same sessions
    pc = {}
    for k in ("seconds", "output_tokens", "cost_usd", "cache_creation_input_tokens", "cache_read_input_tokens"):
        pairs_k = [(S[o].get(k), P[o].get(k)) for o in both if S[o].get(k) is not None and P[o].get(k) is not None]
        pc[k] = {"S_mean": mean(x for x, _ in pairs_k), "P_mean": mean(y for _, y in pairs_k), "mean_diff": mean(y - x for x, y in pairs_k),
                 "ratio": (sum(y for _, y in pairs_k) / sum(x for x, _ in pairs_k)) if pairs_k and sum(x for x, _ in pairs_k) else None}
    pc["S_all_calls"] = {k: dist([S[o].get(k) for o in rows if S[o].get(k) is not None]) for k in ("seconds", "output_tokens", "cost_usd")}
    res["per_call"] = pc
    # ---- reading file for the fca70973 wish-list check (the eval owner reads it; the result is recorded as present or absent, not computed)
    rdir = os.path.join(out_root, "stage_x0c_reading")
    os.makedirs(rdir, exist_ok=True)
    out_read = {}
    for o in rows:
        if "fca70973" in rows[o]["questions"] and ok(S[o]) and ok(P[o]):
            out_read[o] = {"S_user_facts": [{k: f.get(k) for k in ("round", "subject", "predicate", "object", "quote")} for f in S[o]["facts"] if f.get("author") == "user"],
                           "P_user_facts": [{k: f.get(k) for k in ("round", "subject", "predicate", "object", "quote")} for f in P[o]["facts"] if f.get("author") == "user"]}
    json.dump(out_read, open(os.path.join(rdir, "fca70973_user_facts.json"), "w"), indent=1)
    res["wish_list_reading_file"] = "out/stage_x0c_reading/fca70973_user_facts.json (read by the eval owner; recorded as present or absent per item, not scored by code)"
    json.dump(res, open(out_json, "w"), indent=1)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:4])
