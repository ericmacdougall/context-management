"""I4 recorded decisions + I5 acted-on version, 2000 generated scenarios, independent truth model, broken-variant controls.
usage: i4_i5.py [scenarios=2000]   (pure Python, no database)"""
import json, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import decisions as D, scen_decisions as G

RES = os.environ.get("RESDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))


def run(n, broken_state=None, broken_acted=None, seed=16096):
    rng = random.Random(seed)
    f = {}; tot = {"scenarios": 0, "events": 0, "detected": 0, "rejected": 0, "reverts_adjacent": 0, "disputed_observations": 0, "unresolved_observations": 0, "unauthorized": 0, "decisions": 0, "stale_work_items": 0, "stale_contexts": 0, "prefix_checks": 0}
    def fail(k): f[k] = f.get(k, 0) + 1
    for k in range(n):
        sc = G.gen(rng, immediate_revert=(k % 3 == 0)); evs = sc["events"]
        tot["scenarios"] += 1; tot["events"] += len(evs)
        s = D.initial(); prev_core = None; cores = []
        for i, e in enumerate(evs):
            D.apply(s, e, inplace=True, broken=broken_state)
            c = D.core(s) if s["version"] else {}
            cores.append(c)
            tot["prefix_checks"] += 1
            if c != sc["truth_core"][i]:
                fail("state_mismatch_after_event")
            if e["kind"] == "change_detected" and prev_core is not None:
                tot["detected"] += 1
                if {i2: v[0] for i2, v in c.items()} != {i2: v[0] for i2, v in prev_core.items()}:
                    fail("detected_change_altered_current_definition")
            prev_core = c
            st = c and [v[1] for v in c.values()]
            if st:
                tot["disputed_observations"] += "disputed" in st; tot["unresolved_observations"] += "unresolved" in st
        # P2: a rejected change leaves no trace in current state but stays in history
        for di, ri, cid in sc["reject_events"]:
            rid = evs[ri]["body"]["decision_id"]
            rbs = {e["body"]["read_back_id"] for e in evs[:ri + 1] if e["kind"] == "read_back" and cid in e["body"]["includes"]}
            touched_elsewhere = any(j not in (di, ri) and ((e["body"].get("change_id") == cid) or e["body"].get("reverts") == rid or (e["kind"] == "decision_recorded" and e["body"].get("read_back_id") in rbs))
                                    for j, e in enumerate(evs[:ri + 1]))
            if touched_elsewhere:
                tot["rejected_with_later_interactions_skipped_in_trace_check"] = tot.get("rejected_with_later_interactions_skipped_in_trace_check", 0) + 1
                if D.fold(evs[:ri + 1], broken_state)["history"].get(cid, {}).get("disposition") not in ("rejected", "pending", "accepted", "accepted_no_effect"):
                    fail("history_lost_change")
                continue
            tot["rejected"] += 1
            e2 = [dict(e) for j, e in enumerate(evs[:ri + 1]) if j != di and j != ri]
            for e in e2:
                if e["kind"] == "read_back":
                    e["body"] = dict(e["body"], includes=[c for c in e["body"]["includes"] if c != cid])
            ref = D.fold(e2)
            if D.core(ref) != cores[ri]:
                fail("rejected_change_left_a_trace_in_current_state")
            if D.fold(evs[:ri + 1], broken_state)["history"].get(cid, {}).get("disposition") != "rejected":
                fail("rejected_change_missing_from_history")
        # P4: adjacent revert restores the prior state exactly
        for before, after in sc["revert_pairs"]:
            tot["reverts_adjacent"] += 1
            if before >= 0 and cores[before] != cores[after]:
                fail("revert_did_not_restore_prior_state")
        # unauthorized decisions recorded, none applied
        tot["unauthorized"] += len(s["unauthorized"]) if True else 0
        if len(s["unauthorized"]) != sc["unauthorized"]:
            fail("unauthorized_decision_count")
        tot["decisions"] += len(s["decisions"])
        # P5: rebuild identical incl. persisted-and-reloaded incremental state
        inc = D.initial()
        for e in evs:
            D.apply(inc, e, inplace=True, broken=broken_state)
            if rng.random() < 0.1:
                inc = json.loads(D.canon(inc))
        if D.canon(D.view(inc)) != D.canon(D.view(s)) or D.canon(D.view(D.fold(evs, broken_state))) != D.canon(D.view(s)):
            fail("rebuild_not_identical")
        # I5 exactness vs the snapshot-based oracle (end of scenario)
        sw = [(a, b, c, d, e) for a, b, c, d, e in D.stale_work(s, broken_acted)]
        sx = [(a, b, c, d) for a, b, c, d in D.stale_contexts(s, broken_acted)]
        tw = [tuple(x) for x in sc["stale_work"]]; tx = [tuple(x) for x in sc["stale_contexts"]]
        tot["stale_work_items"] += len(tw); tot["stale_contexts"] += len(tx)
        if sw != tw: fail("stale_work_list_not_exact")
        if sx != tx: fail("stale_contexts_list_not_exact")
    return {"totals": tot, "failures": f}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    out = {"workload": f"{n} generated scenarios (seed 16096): 3-10 items, 15-60 actions of 12 kinds (discussion, detected changes incl. conflicting, read-backs, accept/reject/state/accept-read-back/revert decisions, unauthorised decisions from assistant/web/pasted, steps and injections that record a possibly stale (version, watermark)); per-event state compared with an independent truth model"}
    t0 = time.time()
    out["correct"] = run(n)
    out["broken_detected_applies"] = run(n, broken_state="detected_applies")
    out["broken_assistant_decides"] = run(n, broken_state="assistant_decides")
    out["broken_reject_leaves_trace"] = run(n, broken_state="reject_leaves_trace")
    out["broken_acted_on_version_compare"] = run(n, broken_acted="version_compare")
    out["broken_acted_on_ignores_serves"] = run(n, broken_acted="ignore_serves")
    out["elapsed_s"] = round(time.time() - t0, 1)
    c = out["correct"]
    out["pass"] = (not c["failures"] and c["totals"]["reverts_adjacent"] > 100 and c["totals"]["rejected"] > 100 and c["totals"]["disputed_observations"] > 100
                   and all(out[k]["failures"] for k in out if k.startswith("broken_")))
    os.makedirs(RES, exist_ok=True)
    json.dump(out, open(f"{RES}/I4_I5_decisions.json", "w"), indent=1)
    print(json.dumps({k: (v if not isinstance(v, dict) else {"totals": v.get("totals"), "failures": v.get("failures")}) for k, v in out.items() if k != "workload"}, indent=1))
    print("PASS" if out["pass"] else "FAIL")


if __name__ == "__main__":
    main()
