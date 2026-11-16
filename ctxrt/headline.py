"""Builds ctxrt/results/HEADLINE.json: the final stamped results at one commit, the criteria that are NOT PROVEN, every earlier failed run, and the deviations."""
import json, os, glob
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "ctxrt", "results")
out = {"final_runs": {}, "not_proven": [], "earlier_failed_runs": [], "deviations": []}
for n in ("p1_trim", "p2_compact", "p3_pull", "p4_analyst", "p4_process", "p5_delivery", "p6_routing", "p7_chain"):
    p = os.path.join(RES, n + ".json")
    if not os.path.exists(p):
        out["final_runs"][n] = "MISSING"
        continue
    d = json.load(open(p))
    fails = sorted({(c["criterion"]) for ps in d["per_seed"] for c in ps["criteria"] if not c["ok"]})
    out["final_runs"][n] = {"overall": d["overall"], "code_commit": d["stamp"]["code_commit"], "time_utc": d["stamp"]["time_utc"], "loadavg": d["stamp"]["loadavg"], "failed_criteria": fails}
    for c in d["verdict_by_criterion"]:
        if "NOT PROVEN" in c:
            out["not_proven"].append({"proof": n, "criterion": c, "verdict_on_stub": d["verdict_by_criterion"][c], "status": "NOT PROVEN until the policy rescue lands"})
for d in sorted(glob.glob(os.path.join(RES, "run1-*"))):
    for f in glob.glob(os.path.join(d, "*.json")):
        j = json.load(open(f))
        fails = sorted({(c["criterion"]) for ps in j["per_seed"] for c in ps["criteria"] if not c["ok"]})
        out["earlier_failed_runs"].append({"dir": os.path.basename(d), "proof": j["proof"], "overall": j["overall"], "code_commit": j["stamp"]["code_commit"], "failed_criteria": fails})
out["earlier_failed_runs"].append({"note": "earlier all-PASS runs of P1-P7 at bd92199, 6cfb902, a194525, 27838be, a0b885b are superseded by the rerun above and were judged BLOCK by research/ctxrt-review-1d19cd8.md; their files are in git history at 1d19cd8"})
out["deviations"] = [
    "P5 policy arm ran about 133 of 200 tightened cases per seed in the 1d19cd8 results (now 200 plus 50 canaries); still NOT PROVEN (policy point unrescued)",
    "4f synchronous control was changed after it failed in 3 of 5 seeds; p4_process readiness wait added after the witness failed in 2 seeds; the 4f latency criterion is dropped (a 1 ms stub makes it meaningless)",
    "4b C1 control was arithmetic in the first runs and was rebuilt as a real broken gateway",
    "P1 budgets draw from 0.3 x the live turn (first runs: 0.6 x the tail); over-budget cases are flagged and still valid",
    "recompute.py is a second implementation by the same author, not independent evidence; P4 has no raw re-judgment",
    "one in-process SQLite connection behind a lock for all concurrency proofs except p4_process"]
json.dump(out, open(os.path.join(RES, "HEADLINE.json"), "w"), indent=1)
print(json.dumps(out["final_runs"], indent=1))
