"""Independent recomputation of the B2 verdict from its per-item rows (stdlib only; imports nothing from pol or exp).

From the raw per-event rows (reference and production outcome codes, original codes, view hashes), the per-log rows (live and rebuild
snapshot hashes, production codes, the reference's audit codes), the service-audit rows (codes returned by DecisionService.submit and
refusal reasons found in the AuditLog) and the control rows (first mismatch with both full views, live and rebuild hashes).

Evidence identity (GoodQA #16096 class B, verify/evidence.py): params.per_seed, modes and controls must equal the pins (THRESHOLDS B2:
600 logs per seed, at least 5 seeds) and params.seeds is the seed list; the logs are "<seed>/<li>", li = 0..per_seed-1, in that order.
The rows must be EXACTLY: one enforced log row per log; its event rows i = 0..n-1 where n is declared by the log row (category 2,
parent-declared); one svc_audit row for every fifth log of the run order (logs[::5]); one ctl_log row per (control, log). The per-log
event codes are read in event order (i), never in row order. Anything else FAILs before any count is read."""
import importlib.util
import json
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()

APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
# amendment 5: the amendment-4 floor exception went with the value-unchanged N3 control; every control's floor is 10 percent
FLOORS = {}
# round 3 (amendment 5): self_approval and no_value_change are gone with rule 11 (non-humans never decide)
REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch",
           "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal",
           "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve", "not_contested",
           "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
SERVICE_REFUSALS = REFUSED + ("identity_in_body", "unknown_tenant")
# the design (THRESHOLDS B2; exp/b2_rebuild.py PER_SEED, MODES, DIFF_CONTROLS + SNAP_CONTROLS): pinned here, never read from the rows
PER_SEED, MIN_SEEDS = 600, 5
MODES = [["random", 0.70], ["cap", 0.15], ["contest", 0.15]]
CONTROLS = ["agent_may_decide", "n2_no_cap", "n2_no_role", "m2_no_original", "stale_unchecked", "agent_may_supersede", "snapshot_drops_revocations",
            "restore_drops_idempotency"]
KINDS = ("event", "log", "svc_audit", "ctl_log")


def _c(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _gate(doc, rows):
    g = EV.Gate("b2_rebuild", doc)
    g.pinned({"per_seed": PER_SEED, "modes": MODES, "controls": CONTROLS})
    sd = g.seeds(MIN_SEEDS)
    ids = ["%s/%d" % (s, li) for s in sd for li in range(PER_SEED)]
    fam = g.split(rows, lambda r: r["kind"] if r["kind"] in KINDS else None)
    logs = g.family("log", fam.get("log", []), lambda r: (r["arm"], r["log"]), {EV.key("enforced", lid) for lid in ids})
    if logs is None:
        g.not_checked("event", "the log rows are not exact")
    else:
        g.family("event", fam.get("event", []), lambda r: (r["arm"], r["log"], r["i"]),
                 {EV.key("enforced", r["log"], i) for k, r in logs.items() for i in range(g.declared_count("event", k, r.get("n")))})
    g.family("svc_audit", fam.get("svc_audit", []), lambda r: (r["log"],), {EV.key(lid) for lid in ids[::5]})
    g.family("ctl_log", fam.get("ctl_log", []), lambda r: (r["arm"], r["log"]), {EV.key(a, lid) for a in CONTROLS for lid in ids})
    return g


def verify(doc, rows):
    res = doc["result"]
    g = _gate(doc, rows)
    if not g.ok:
        return g.fail(agrees=res.get("verdict") == "FAIL")
    ev = [r for r in rows if r["kind"] == "event" and r["arm"] == "enforced"]
    logs = [r for r in rows if r["kind"] == "log" and r["arm"] == "enforced"]
    bad_events = sum(1 for r in ev if (r["ref_code"], r["ref_orig"], r["ref_view"]) != (r["prod_code"], r["prod_orig"], r["prod_view"]))
    codes = {}
    for r in ev:
        codes[r["prod_code"]] = codes.get(r["prod_code"], 0) + 1
    per_log_events = {}
    for r in sorted(ev, key=lambda r: r["i"]):         # event order, not row order (exact set: the i are 0..n-1 per log)
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
    ctl_rep = {k: {"logs": n, "mismatch": b, "rate": b / n if n else 0.0, "meets_floor": n > 0 and b / n >= FLOORS.get(k, 0.10)} for k, (n, b) in ctl.items()}
    ctl_ok = bool(ctl_rep) and all(v["meets_floor"] for v in ctl_rep.values()) and set(ctl_rep) == set(res.get("controls", {}))
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res["verdict"],
            "checks": {"evidence": g.report(), "logs": len(logs), "events": len(ev), "mismatched_events": bad_events, "log_codes_consistent": log_codes_ok, "rebuild_unequal": rebuild_bad,
                       "ref_audit_bad_logs": ref_audit_bad, "codes_missing": missing, "contested_logs": contested, "restores": restores,
                       "service_audit_logs": len(svc), "service_audit_bad": svc_bad, "service_refusals": svc_ref, "controls": ctl_rep}}
