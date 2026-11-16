"""Independent recomputation of the B3 verdict from its per-item rows (stdlib only; imports nothing from pol or exp).

Each scenario round is re-judged from its recorded outcome codes and head fields (not from the experiment's ok flag); the overlap
witness is re-derived from the recorded database timestamps; the differential arm is re-judged from per-event memory/Postgres codes and
the per-log views, chain result and audit count (refusals recounted here from the Postgres codes)."""
import datetime
import json

# round 3 (amendment 5): self_approval and no_value_change are gone with rule 11 (non-humans never decide)
REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch",
           "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal",
           "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve", "not_contested",
           "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
OVERLAP = ("S1_conflicting_decisions_contested", "S3_zombie_revoked_principal", "S4_same_idem_key_concurrent", "S4b_twins_on_two_requirements", "S5_resolve_race")
API_OVERLAP = ("S7_api_supersede_race", "S8_api_twins")        # deployment path (dc_app + dc.submit): B blocked < A commit < B done
SCENARIOS = ("S1_conflicting_decisions_contested", "S2_stale_token_refused", "S3_zombie_revoked_principal", "S4_same_idem_key_concurrent",
             "S4b_twins_on_two_requirements", "S5_resolve_race", "S6_tenant_isolation") + API_OVERLAP


def _t(s):
    return datetime.datetime.fromisoformat(s) if s else None


def _ov(w, full=True):
    a, b, c, d = (_t(w.get(k)) for k in ("a_locked", "b_blocked", "a_before_commit", "b_locked"))
    if full:
        return None not in (a, b, c, d) and a < b < c < d
    return None not in (a, b, c) and a < b < c


def _ov_api(w):
    b, a, d = (_t(w.get(k)) for k in ("b_blocked", "a_commit", "b_done"))
    return None not in (a, b, d) and b < a < d and w.get("b_done_before_commit") == "False"


def _codes(info):
    return {k: tuple(v) if v is not None else None for k, v in (info.get("codes") or {}).items()}


def judge(scen, i, info):
    c = _codes(info) if isinstance(info.get("codes"), dict) else {}
    if info.get("errs"):
        return False
    if scen == "S1_conflicting_decisions_contested":
        return (c.get("a") == ("decided_accept", None) and c.get("b") == ("contested_opened", None) and info["status"] == "contested"
                and info["version"] == 2 and info["value_ref"] is None and len(info["candidates"]) == 2)
    if scen == "S2_stale_token_refused":
        return c.get("a") == ("superseded", None) and c.get("b") == ("stale", None) and info["version"] == 3 and info["value_ref"] == "vY"
    if scen == "S3_zombie_revoked_principal":
        return c.get("a") == ("revoked", None) and c.get("b") == ("principal_revoked", None) and info["version"] == 0
    if scen == "S4_same_idem_key_concurrent":
        want = ("duplicate", "superseded") if i % 2 == 0 else ("idem_conflict", "superseded")
        return c.get("a") == ("superseded", None) and c.get("b") == want and info["event_rows"] == 1
    if scen == "S4b_twins_on_two_requirements":
        return c.get("a") == ("superseded", None) and c.get("b") == ("idem_conflict", "superseded") and info["event_rows"] == 1
    if scen == "S5_resolve_race":
        return c.get("a") == ("resolved", None) and c.get("b") == ("stale", None) and info["version"] == 3
    if scen == "S6_tenant_isolation":
        return [x[0] for x in info["codes"]] == ["unauthorized", "tenant_mismatch", "unauthorized"]
    if scen == "S7_api_supersede_race":
        return (c.get("a") == ("superseded", None) and c.get("b") == ("contested_opened", None) and info["status"] == "contested"
                and info["version"] == 2 and info["candidates"] == ["u1/Sa%d" % i, "u2/Sb%d" % i])
    if scen == "S8_api_twins":
        return c.get("a") == ("superseded", None) and c.get("b") == ("duplicate", "superseded") and info["event_rows"] == 1
    if scen == "ctl_no_head_lock":
        return c.get("a") == ("decided_accept", None) and c.get("b") == ("decided_accept", None) and info["status"] == "current"
    if scen == "ctl_no_advisory_lock":
        return info["backstop_retries"] >= 1
    if scen == "ctl_idem_unprotected":
        return c.get("a") == ("superseded", None) and c.get("b") == ("superseded", None) and info["event_rows"] == 2
    raise ValueError(scen)


def verify(doc, rows):
    res = doc["result"]
    R = doc["params"]["R"]
    sc = {}
    for r in rows:
        if r["kind"] != "round":
            continue
        s = sc.setdefault(r["scenario"], {"rounds": 0, "passed": 0, "overlap": 0})
        s["rounds"] += 1
        try:
            s["passed"] += bool(judge(r["scenario"], r["round"], r["info"]))
        except (KeyError, TypeError):
            pass
        w = r["info"].get("w") or {}
        if r["scenario"] in OVERLAP:
            s["overlap"] += _ov(w)
        elif r["scenario"] in API_OVERLAP:
            s["overlap"] += _ov_api(w)
        elif r["scenario"] in ("ctl_no_head_lock", "ctl_no_advisory_lock"):
            s["overlap"] += _ov(w, full=False)
    scen_ok = all(sc.get(k, {}).get("passed") == R for k in SCENARIOS)
    ov_ok = all(sc.get(k, {}).get("overlap") == R for k in OVERLAP + API_OVERLAP)
    dev = [r for r in rows if r["kind"] == "diff_event"]
    dlog = [r for r in rows if r["kind"] == "diff_log"]
    ev_bad = sum(1 for r in dev if r["mem"] != r["pg"])
    refused = {}
    for r in dev:
        refused[r["log"]] = refused.get(r["log"], 0) + (r["pg"][0] in REFUSED)
    log_bad = sum(1 for r in dlog if not (json.dumps(r["pg_view"], sort_keys=True) == json.dumps(r["mem_view"], sort_keys=True)
                                          == json.dumps(r["rebuild_view"], sort_keys=True) and r["chain"][1] and r["chain"][0] == r["applied_rows"]
                                          and r["audit_rows"] == refused.get(r["log"], 0)))
    sr = [r for r in rows if r["kind"] == "scen_rebuild"]
    reb_ok = len(sr) == 1 and json.dumps(sr[0]["live"], sort_keys=True) == json.dumps(sr[0]["rebuild"], sort_keys=True) and sr[0]["chain"][1] \
        and sr[0]["chain"][0] == sr[0]["applied_rows"]
    ok = scen_ok and ov_ok and ev_bad == 0 and log_bad == 0 and len(dlog) > 0 and reb_ok
    floor = int(0.9 * R)
    ctl_ok = all(sc.get(k, {}).get("passed", 0) >= floor for k in ("ctl_no_head_lock", "ctl_no_advisory_lock", "ctl_idem_unprotected")) \
        and sc.get("ctl_no_head_lock", {}).get("overlap", 0) >= floor
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res["verdict"],
            "checks": {"scenarios": sc, "diff_events": len(dev), "diff_event_mismatch": ev_bad, "diff_logs": len(dlog), "diff_log_bad": log_bad,
                       "scenario_rebuild_ok": reb_ok}}
