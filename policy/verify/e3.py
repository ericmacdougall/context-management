"""Independent recomputation of E3 from its raw rows (stdlib only; imports nothing from pol or exp).

Acceptance is decided here from what came back:
  pdp rows (planted, fault, context, legit): accepted when returned.allow is exactly True; an escaping exception ("raised") is a
    failure of the enforced arm in its own right.
  forge rows: by entry point, from the returned observation (an id, a text, a payload, a queued item, an applied decision code, a
    session that verifies while claiming the victim's identity, a victim session ended by someone else, the victim's identity
    returned, a marker in a read, an erasure that was not refused).

Evidence identity (GoodQA #16096 class B, verify/evidence.py): params must equal PINS (THRESHOLDS E3), params.seeds is the seed list and
params.surface_entries (the experiment's surface walk; the verifier cannot import it) must name every entry of KNOWN_ENTRIES, each once.
The rows must be EXACTLY, per seed: one PDP row per (arm, cat, seed, case), case = 0..PINS[cat]-1, for the arms of PDP_ARMS[cat]; one
forge row per (arm, "forge", seed, round, group, entry, forge) for round = 0..forge_rounds-1: session forgeries SESSION_FORGES on every
declared entry (enforced) and every declared entry outside pol.decisions (ctl_no_session_verify), request forgeries REQ_FORGES on
REQ_ENTRY (enforced, ctl_naive_pdp), body forgeries BODY_FORGES on BODY_ENTRY (enforced, ctl_body_identity_trusted). Anything else FAILs
before any counter is read. Limit: entries beyond KNOWN_ENTRIES are taken as declared (an unknown entry's observation counts as accepted)."""
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

APPLIED = {"proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted"}
FORGERY_REASONS = {"attr_unrecognised_key", "forged_request", "attr_source_type"}
# the design (THRESHOLDS E3; exp/e3_clash.py): pinned here, never read from the judged rows
PINS = {"planted": 2200, "fault": 2400, "legit": 1400, "context": 3000, "forge_rounds": 10}
MIN_SEEDS = 1
PDP_ARMS = {"planted": ("enforced", "ctl_naive_pdp"), "fault": ("enforced", "ctl_naive_pdp"), "legit": ("enforced",),
            "context": ("enforced", "ctl_naive_pdp")}
SESSION_FORGES = ("handbuilt", "attacker_token_victim_id", "victim_token_copy", "principal_object", "none", "string", "dict", "ended_victim_session",
                  "attacker_real_session", "other_tenant_session", "namespace_obj")
REQ_FORGES = ("tenant", "principal", "recipient_obj", "now", "region", "cred_tenant", "cred_clearance", "cred_groups", "cred_confusable", "cred_case",
              "cred_space", "cred_zero_width", "viewer", "trusted", "labels")
BODY_FORGES = ("body_tenant", "body_principal", "body_channel", "body_id", "body_value_ref", "body_namespaced_client_id", "body_traversal_client_id",
               "body_revoke_victim", "body_grant_self", "body_unknown_key")
REQ_ENTRY, BODY_ENTRY = "pol.enforce.Outbox.enqueue", "pol.decisions.DecisionService.submit"
KNOWN_ENTRIES = ("pol.decisions.DecisionService.erase_subject", "pol.decisions.DecisionService.explain", "pol.decisions.DecisionService.submit",
                 "pol.decisions.DecisionService.table", "pol.directory.SessionAuthority.end", "pol.directory.SessionAuthority.verify",
                 "pol.enforce.Enforcer.principal_of", "pol.enforce.GuardedChat.complete", "pol.enforce.GuardedEmbedder.embed",
                 "pol.enforce.GuardedExport.export", "pol.enforce.Outbox.enqueue", "pol.enforce.Store.append", "pol.enforce.Store.append_derived",
                 "pol.enforce.Store.context", "pol.enforce.Store.erase", "pol.enforce.Store.read")


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


def _entries(g):
    se = g.params.get("surface_entries")
    try:
        names = [e["entry"] for e in se]
        assert names and all(isinstance(n, str) for n in names)
    except Exception:                   # noqa: BLE001  not a non-empty list of {"entry": str}
        g.problem("params.surface_entries is not a non-empty list of {entry: str}")
        return []
    if len(set(names)) != len(names):
        g.problem("params.surface_entries repeats an entry")
        return []
    lost = sorted(set(KNOWN_ENTRIES) - set(names))
    if lost:
        g.problem("params.surface_entries lacks %d pinned entr(y/ies): %s" % (len(lost), ", ".join(lost[:8])))
    return names


def _forge_expected(sd, ents):
    out = set()
    for s in sd:
        for rnd in range(PINS["forge_rounds"]):
            for e in ents:
                for fk in SESSION_FORGES:
                    out.add(EV.key("enforced", "forge", s, rnd, "session", e, fk))
                    if not e.startswith("pol.decisions"):
                        out.add(EV.key("ctl_no_session_verify", "forge", s, rnd, "session", e, fk))
            out |= {EV.key(a, "forge", s, rnd, "request", REQ_ENTRY, fk) for a in ("enforced", "ctl_naive_pdp") for fk in REQ_FORGES}
            out |= {EV.key(a, "forge", s, rnd, "body", BODY_ENTRY, fk) for a in ("enforced", "ctl_body_identity_trusted") for fk in BODY_FORGES}
    return out


def _gate(result_doc, rows):
    g = EV.Gate("e3_clash", result_doc)
    g.pinned(PINS)
    sd = g.seeds(MIN_SEEDS)
    ents = _entries(g)
    fam = g.split(rows, lambda r: "forge" if r["cat"] == "forge" else "pdp" if r["cat"] in PDP_ARMS else None)
    g.family("pdp", fam.get("pdp", []), lambda r: (r["arm"], r["cat"], r["seed"], r["case"]),
             {EV.key(a, c, s, i) for c, arms in PDP_ARMS.items() for a in arms for s in sd for i in range(PINS[c])})
    g.family("forge", fam.get("forge", []), lambda r: (r["arm"], r["cat"], r["seed"], r["round"], r["group"], r["entry"], r["forge"]),
             _forge_expected(sd, ents))
    return g


def verify(result_doc, rows):
    g = _gate(result_doc, rows)
    if not g.ok:
        return g.fail()
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
            "checks": {"evidence": g.report(), "counts_n_accepted": cnt, "control_rates": rates, "escaped_exceptions": raised, "planted_wrong_reason": wrong_reason,
                       "uncovered_entries": missing, "n_surface_entries": len(entries), "sizes_ok": sizes_ok, "seeds": n_seeds}}
