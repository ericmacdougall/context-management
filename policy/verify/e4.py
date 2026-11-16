"""Independent recomputation of the E4 verdict from its raw rows (stdlib only; imports nothing from pol or exp).

Per run: enqueue decision, audit rows after the tightening, deliveries, refusal reasons, sent payloads (marker present, recipient
object identity sent vs evaluated). Thresholds: THRESHOLDS.md E4 with amendments 1-3."""

POLICY_CHANGES = {"model_banned", "region_removed", "sender_revoked", "recipient_revoked", "tenant_killed"}


def verify(doc, rows):
    by = {}
    for r in rows:
        by.setdefault(r["arm"], []).append(r)
    f, p, c = by.get("fenced", []), by.get("positive", []), by.get("control_enqueue_only", [])
    checks = {}
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
    verdict = "PASS" if all(checks.values()) else "FAIL"
    return {"verdict": verdict, "checks": checks, "agrees_with_experiment": verdict == doc["result"]["verdict"], "control_delivery_rate": ctl_rate}
