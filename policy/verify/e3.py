"""Independent recomputation of E3 from its raw rows (stdlib only; imports nothing from pol or exp).

Acceptance is decided here from what came back:
  pdp rows (planted, fault, context, legit): accepted when returned.allow is exactly True; an escaping exception ("raised") is a
    failure of the enforced arm in its own right.
  forge rows: by entry point, from the returned observation (an id, a text, a payload, a queued item, an applied decision code, a
    session that verifies while claiming the victim's identity, a victim session ended by someone else, the victim's identity
    returned, a marker in a read, an erasure that was not refused)."""
import json

APPLIED = {"proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted"}
FORGERY_REASONS = {"attr_unrecognised_key", "forged_request", "attr_source_type"}


def forge_accepted(r):
    x = r["returned"]
    if x.get("type") != "value":
        return False
    e = r["entry"].split(".", 2)[2]
    victim = r.get("victim")
    if e in ("Store.read",):
        return x.get("text") is not None
    if e in ("Store.append", "Store.append_derived"):
        return x.get("id") is not None
    if e == "Store.context":
        return len(x.get("ok") or []) > 0
    if e == "Store.erase":
        return x.get("value") in ("erased", "queued")
    if e in ("GuardedChat.complete", "GuardedEmbedder.embed", "GuardedExport.export"):
        return len(x.get("payloads") or []) > 0
    if e == "Outbox.enqueue":
        return x.get("value") == "queued"
    if e == "Enforcer.principal_of":
        return x.get("principal") == victim
    if e == "SessionAuthority.verify":
        return x.get("value") is True and r.get("session_principal_id") == victim
    if e == "SessionAuthority.end":
        return x.get("victim_session_still_valid") is False
    if e == "DecisionService.submit":
        return x.get("code") in APPLIED
    if e == "DecisionService.erase_subject":
        v = x.get("value") or {}
        return v.get("refused") is None
    if e in ("DecisionService.explain", "DecisionService.table"):
        return "MARK-" in str(x.get("value"))
    return True             # an observation for an entry this verifier does not know counts as accepted (fail closed)


def verify(result_doc, rows):
    params = result_doc.get("params", {})
    cnt = {}
    raised = 0
    wrong_reason = 0
    for r in rows:
        if r["cat"] == "forge":
            k = "%s/forge_%s" % (r["arm"], r["group"])
            a = forge_accepted(r)
        else:
            k = "%s/%s" % (r["arm"], r["cat"])
            a = r["returned"]["allow"] is True
            if r["arm"] == "enforced" and r["returned"]["allow"] == "raised":
                raised += 1
            if r["arm"] == "enforced" and r["cat"] == "planted" and r["returned"]["reason"] not in FORGERY_REASONS:
                wrong_reason += 1
        c = cnt.setdefault(k, [0, 0])
        c[0] += 1
        c[1] += a
    entries = {e["entry"] for e in params.get("surface_entries", [])}
    covered = {r["entry"] for r in rows if r["arm"] == "enforced" and r["cat"] == "forge" and r["returned"].get("type") != "no_driver"}
    missing = sorted(entries - covered)
    n_seeds = max(1, len(params.get("seeds", [1])))
    legit = cnt.get("enforced/legit", [0, 0])
    need = {"enforced/planted": 2000, "enforced/fault": 2000, "enforced/legit": 1000, "enforced/context": 3000}
    sizes_ok = all(cnt.get(k, [0])[0] >= v for k, v in need.items())
    ok = (all(v[1] == 0 for k, v in cnt.items() if k.startswith("enforced/") and k != "enforced/legit") and legit[1] >= 0.99 * legit[0]
          and raised == 0 and wrong_reason == 0 and not missing and bool(entries) and sizes_ok)
    floors = ("ctl_naive_pdp/planted", "ctl_naive_pdp/fault", "ctl_naive_pdp/context", "ctl_naive_pdp/forge_request",
              "ctl_no_session_verify/forge_session", "ctl_body_identity_trusted/forge_body")
    rates = {k: round(cnt[k][1] / max(1, cnt[k][0]), 4) if k in cnt else 0.0 for k in floors}
    ok_ctl = all(v >= 0.10 for v in rates.values())
    return {"verdict": "PASS" if ok and ok_ctl else "FAIL",
            "checks": {"counts_n_accepted": cnt, "control_rates": rates, "escaped_exceptions": raised, "planted_wrong_reason": wrong_reason,
                       "uncovered_entries": missing, "n_surface_entries": len(entries), "sizes_ok": sizes_ok, "seeds": n_seeds}}
