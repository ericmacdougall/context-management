"""Independent recomputation of the B2 verdict from its per-item rows (stdlib only; imports nothing from pol or exp).

From the raw per-event rows (reference and production outcome codes, original codes, view hashes), the per-log rows (live and rebuild
snapshot hashes, production codes, the reference's audit codes), the service-audit rows (codes returned by DecisionService.submit and
refusal reasons found in the AuditLog) and the control rows (first mismatch with both full views, live and rebuild hashes)."""
import json

APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch",
           "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal",
           "self_approval", "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve", "not_contested",
           "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
SERVICE_REFUSALS = REFUSED + ("identity_in_body", "unknown_tenant")


def _c(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def verify(doc, rows):
    res = doc["result"]
    ev = [r for r in rows if r["kind"] == "event" and r["arm"] == "enforced"]
    logs = [r for r in rows if r["kind"] == "log" and r["arm"] == "enforced"]
    bad_events = sum(1 for r in ev if (r["ref_code"], r["ref_orig"], r["ref_view"]) != (r["prod_code"], r["prod_orig"], r["prod_view"]))
    codes = {}
    for r in ev:
        codes[r["prod_code"]] = codes.get(r["prod_code"], 0) + 1
    per_log_events = {}
    for r in ev:
        per_log_events.setdefault(r["log"], []).append(r["prod_code"])
    log_codes_ok = all(per_log_events.get(l["log"], []) == l["prod_codes"] for l in logs)
    rebuild_bad = sum(1 for l in logs if l["live_sha"] != l["rebuild_sha"])
    ref_audit_bad = sum(1 for l in logs if l["ref_audit_codes"] != [c for c in l["prod_codes"] if c in REFUSED])
    contested = sum(1 for l in logs if "contested_opened" in l["prod_codes"])
    restores = sum(l["restores"] for l in logs)
    missing = sorted(set(APPLIED + REFUSED) - set(codes))
    svc = [r for r in rows if r["kind"] == "svc_audit"]
    svc_bad = sum(1 for r in svc if [c for c in r["returned"] if c in SERVICE_REFUSALS] != r["audited_refusals"])
    svc_ref = sum(len([c for c in r["returned"] if c in SERVICE_REFUSALS]) for r in svc)
    ok = (len(logs) >= 3000 and bad_events == 0 and log_codes_ok and rebuild_bad == 0 and ref_audit_bad == 0 and not missing
          and contested >= 0.10 * len(logs) and restores >= 3000 and svc and svc_bad == 0 and svc_ref > 0)
    ctl = {}
    for r in rows:
        if r["kind"] != "ctl_log":
            continue
        m = r["first_mismatch"]
        mism = m is not None and (m["ref"] != m["prod"] or _c(m["ref_view"]) != _c(m["prod_view"]))
        c = ctl.setdefault(r["arm"], [0, 0])
        c[0] += 1
        c[1] += bool(mism or r["live_sha"] != r["rebuild_sha"])
    ctl_rep = {k: {"logs": n, "mismatch": b, "rate": b / n if n else 0.0, "meets_floor": n > 0 and b / n >= 0.10} for k, (n, b) in ctl.items()}
    ctl_ok = bool(ctl_rep) and all(v["meets_floor"] for v in ctl_rep.values()) and set(ctl_rep) == set(res.get("controls", {}))
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res["verdict"],
            "checks": {"logs": len(logs), "events": len(ev), "mismatched_events": bad_events, "log_codes_consistent": log_codes_ok, "rebuild_unequal": rebuild_bad,
                       "ref_audit_bad_logs": ref_audit_bad, "codes_missing": missing, "contested_logs": contested, "restores": restores,
                       "service_audit_logs": len(svc), "service_audit_bad": svc_bad, "service_refusals": svc_ref, "controls": ctl_rep}}
