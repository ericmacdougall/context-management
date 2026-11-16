"""Independent recomputation of the B1 verdict from its per-item rows (stdlib only; imports nothing from pol or exp).

Enforced: the per-scenario counters (states, transitions, codes, violations, contested states) are summed and the thresholds applied
here. Controls: the recorded shortest counterexample must be a well-formed event trace whose last event is the recorded step, and the
expected invariant must be violated when recomputed HERE from the raw pre/post snapshots, event, code and original code."""
import hashlib
import json

APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
# round 3 (amendment 5): self_approval and no_value_change are gone with rule 11 (non-humans never decide)
REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch",
           "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal",
           "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve", "not_contested",
           "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
UNREACHABLE = {"candidates_full"}
# amendment 5 (round 3): agent_may_decide replaces the two N3 controls and self_approval_removed; cap_blocks_humans is R3-4 reintroduced
EXPECTED = {"agent_may_decide": "I2_agent_head_move", "cap_blocks_humans": "I18_human_locked_out_by_nonhumans",
            "n2_no_role": "I15_proposal_without_role", "n2_no_cap": "I17_open_cap",
            "stale_unchecked": "I4_decision_not_bound_to_head", "agent_may_supersede": "I2_agent_head_move",
            "m2_no_original": "I6_redelivery_changed_state_or_lost_outcome",
            "idempotency_removed": "I6_redelivery_changed_state_or_lost_outcome", "authority_removed": "I1_authority"}
DECISION_LIKE = ("decided_accept", "decided_amend", "superseded", "retracted", "confirmed_applied", "resolved", "decided_reject", "contested_opened",
                 "contested_joined", "concurrent_agree")
COMMON = ("type", "id", "idem", "tenant", "principal", "channel")


def _c(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _fp(ev):
    return hashlib.sha256(_c(ev).encode()).hexdigest()


def _oh(s, req):
    return len([1 for d in s["props"].get(req, {}).values() if d["kind"] == "proposal" and d["status"] == "open" and d["author_kind"] == "human"])


def violated(name, row, caps=(2, 3)):
    pre, post, ev, code, orig = row["pre"], row["post"], row["ev"], row["code"], row["orig"]
    req, pid = ev.get("req"), ev.get("principal")
    kind = pre["principals"].get(pid)
    if name == "I6_redelivery_changed_state_or_lost_outcome":
        for p, k, fp, c in pre["idems"]:
            if p == pid and k == ev.get("idem") and fp == _fp(ev):
                return code != "duplicate" or orig != c or _c(pre) != _c(post)
        return False
    if name == "I17_open_cap":                        # round 3: the per-requirement cap counts non-human-authored items only
        for r in post["props"]:
            op = [d["author"] for d in post["props"][r].values() if d["status"] == "open"]
            nh = [d for d in post["props"][r].values() if d["status"] == "open" and d["author_kind"] != "human"]
            if len(nh) > caps[1] or any(op.count(a) > caps[0] for a in set(op)):
                return True
        return False
    if name == "I18_human_locked_out_by_nonhumans":
        own = [d for d in pre["props"].get(req, {}).values() if d["status"] == "open" and d["author"] == pid]
        return code == "proposal_cap" and kind == "human" and len(own) < caps[0]
    if req not in pre["heads"]:
        return False
    h0, h1 = pre["heads"][req], post["heads"][req]
    alive = pid not in pre["revoked"]
    if name == "I1_authority":
        return code in APPLIED and code not in ("proposed", "pending_created", "revoked", "granted") and not (pid in pre["authority"][req] and alive)
    if name == "I15_proposal_without_role":
        return code in ("proposed", "pending_created") and not ((pid in pre["authority"][req] or pid in pre["proposers"][req]) and alive)
    if name == "I4_decision_not_bound_to_head":
        return code in ("decided_accept", "decided_amend", "superseded", "retracted", "confirmed_applied", "resolved") and ev.get("based_on") != h0["version"]
    moved = h0["version"] != h1["version"]
    changed = (h0["value_ref"], h0["status"]) != (h1["value_ref"], h1["status"])
    if name == "I2_agent_head_move":                  # round 3: a non-human never moves the head or obtains a decision-like outcome
        return kind not in (None, "human") and (moved or changed or code in DECISION_LIKE)
    if name == "I16_nonhuman_head_move_without_value_change":
        # amendment 3 wording (N3 as the brief states it): a non-human event that changes no value never moves the head
        return kind not in (None, "human") and moved and not changed
    if name == "L2_head_starvation":
        if kind in (None, "human"):
            return False
        a, b = _oh(pre, req), _oh(post, req)
        return b > a or (moved and b > a - 1)
    raise ValueError(name)


def verify(doc, rows):
    res = doc["result"]
    checks = {}
    runs = [r for r in rows if r["kind"] == "run" and r["arm"] == "enforced"]
    codes = {}
    for r in runs:
        for k, v in r["codes"].items():
            codes[k] = codes.get(k, 0) + v
    states, trans = sum(r["states"] for r in runs), sum(r["transitions"] for r in runs)
    missing = sorted(set(APPLIED + REFUSED) - set(codes) - UNREACHABLE)
    enforced_cex = [r for r in rows if r["kind"] == "cex" and r["control"] == "enforced"]
    checks["enforced_violations"] = sum(r["violations"] for r in runs)
    checks["enforced_cex_rows"] = len(enforced_cex)
    checks["states"], checks["transitions"], checks["codes_missing"] = states, trans, missing
    checks["contested_states"] = sum(r["contested_states"] for r in runs)
    ok = (checks["enforced_violations"] == 0 and not enforced_cex and states >= 1000 and trans >= 20000 and not missing
          and checks["contested_states"] > 0 and codes.get("resolved", 0) > 0)
    ctl = {}
    for name, inv in EXPECTED.items():
        cx = [r for r in rows if r["kind"] == "cex" and r["control"] == name]
        good = False
        for r in cx:
            tr = r["trace"]
            shape = isinstance(tr, list) and tr and all(isinstance(e, dict) and all(k in e for k in COMMON) for e in tr) and tr[-1] == r["ev"]
            good = good or (bool(shape) and violated(inv, r))
        ctl[name] = {"rows": len(cx), "expected_recomputed": good}
    checks["controls"] = ctl
    ctl_ok = all(c["expected_recomputed"] for c in ctl.values())
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res["verdict"], "checks": checks}
