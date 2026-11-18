"""Independent recomputation of the E4 verdict from its raw rows (stdlib only; imports nothing from pol or exp).

Per run: enqueue decision, audit rows after the tightening, deliveries, refusal reasons, sent payloads (marker present, recipient
object identity sent vs evaluated). Thresholds: THRESHOLDS.md E4 with amendments 1-3.
Evidence (verify/evidence.py, GoodQA #16096 class B): params R and combos EQUAL the pins below (THRESHOLDS amendment 3: the 15
combinations, R = 200 each, 3000 runs per arm), params.seeds holds at least MIN_SEEDS distinct seeds, and the rows are EXACTLY one run per
(arm, combo, seed, i) for the three arms, the 15 combinations and i = 0..R-1, run i on seed seeds[i % len(seeds)] (as the experiment
rotates them). A duplicated, missing, renumbered or undeclared run is a FAIL, never judged."""
import importlib.util
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()
R = 200
COMBOS = [("model", "model_banned"), ("model", "region_removed"), ("model", "sender_revoked"), ("model", "tenant_killed"),
          ("inbox", "model_banned"), ("inbox", "region_removed"), ("inbox", "recipient_revoked"), ("inbox", "tenant_killed"), ("inbox", "group_removed"),
          ("export", "region_removed"), ("export", "sender_revoked"), ("export", "tenant_killed"),
          ("inbox", "label_narrowed"), ("inbox", "source_erased"), ("inbox", "legal_hold_recorded")]
ARMS = ("fenced", "positive", "control_enqueue_only")
MIN_SEEDS = 5
POLICY_CHANGES = {"model_banned", "region_removed", "sender_revoked", "recipient_revoked", "tenant_killed"}


def verify(doc, rows):
    g = EV.Gate("e4_tighten", doc)
    sd = g.seeds(MIN_SEEDS)
    g.pinned({"R": R, "combos": COMBOS})
    want = {EV.key(a, "%s/%s" % c, sd[i % len(sd)], i) for a in ARMS for c in COMBOS for i in range(R)} if sd else set()
    g.family("run", rows, lambda r: (r["arm"], r["combo"], r["seed"], r["i"]), want)
    if not g.ok:
        return g.fail(agrees_with_experiment=doc["result"]["verdict"] == "FAIL")
    by = {}
    for r in rows:
        by.setdefault(r["arm"], []).append(r)
    f, p, c = by.get("fenced", []), by.get("positive", []), by.get("control_enqueue_only", [])
    checks = {"evidence": g.report()}
    checks["fenced_runs_ge_3000"] = len(f) >= 3000 and len({r["combo"] for r in f}) == 15
    checks["fenced_nothing_delivered"] = all(r["delivered"] == 0 and not any(x["text_has_marker"] for x in r["sent"]) for r in f)
    checks["enqueue_allowed_at_v1_all_arms"] = all(r["enqueue"]["allow"] and r["enqueue"]["policy_version"] == 1 for r in f + p + c)

    def refusal_logged(r):
        t = r["combo"].split("/", 1)[1]
        if t == "source_erased":
            return r["refused"] == ["source_erased"] and any(a["action"] == "erase" and a["allow"] for a in r["audit_after"])
        want = 2 if t in POLICY_CHANGES else 1
        return any(not a["allow"] and a["policy_version"] == want for a in r["audit_after"])
    checks["refusal_logged_with_expected_version_all_runs"] = all(refusal_logged(r) for r in f)
    checks["positive_delivers_every_run_and_item"] = len(p) >= 3000 and all(r["delivered"] == 1 and r["sent"] and all(x["text_has_marker"] for x in r["sent"])
                                                                            for r in p)
    checks["recipient_snapshot_same_object"] = all(x["rcp_obj"] == x["checked_rcp_obj"] for r in p if r["combo"].startswith("inbox") for x in r["sent"])
    ctl_rate = sum(1 for r in c if r["delivered"] > 0) / max(1, len(c))
    checks["control_enqueue_only_delivers_ge_90pct"] = ctl_rate >= 0.9
    verdict = "PASS" if all(v for k, v in checks.items() if k != "evidence") else "FAIL"
    return {"verdict": verdict, "checks": checks, "agrees_with_experiment": verdict == doc["result"]["verdict"], "control_delivery_rate": ctl_rate}
