"""Independent recomputation of the B1 verdict from its per-item rows (stdlib only; imports nothing from pol or exp).

Enforced: the per-scenario counters (states, transitions, codes, violations, contested states) are summed and the thresholds applied
here. Controls: the recorded shortest counterexample must be a well-formed event trace whose last event is the recorded step, and the
expected invariant must be violated when recomputed HERE from the raw pre/post snapshots, event, code and original code.

Evidence identity (GoodQA #16096 class B, verify/evidence.py): B1 is seedless. params.depth and params.ctl_depth must equal the pins
below (THRESHOLDS B1, amendments 3-5; the control depth of r1 is 6 since f23f119, amendment 4). The run rows must be EXACTLY one per
(arm, scenario, depth): enforced on every scenario of DEPTH, each control on its CTL_SCENARIOS at CTL_DEPTH. The control counterexample
rows must be exactly one per control (identity: control). Counterexamples of the enforced arm are a verdict condition (any one FAILs),
not an identity family. A duplicate, missing, extra, unidentified or unclassified row FAILs before any counter is read."""
import hashlib
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
# the design (THRESHOLDS B1; exp/b1_modelcheck.py DEPTH, CTL_DEPTH, CONTROLS): pinned here, never read from the judged rows
DEPTH = {"r1": 6, "r2": 8, "r3": 8, "caps": 8, "joint": 4}
CTL_DEPTH = {"r1": 6, "r2": 6, "r3": 6, "caps": 6}
CTL_SCENARIOS = {"agent_may_decide": ["r3"], "cap_blocks_humans": ["caps"], "n2_no_role": ["r2"], "n2_no_cap": ["caps"],
                 "stale_unchecked": ["r1", "r2"], "agent_may_supersede": ["r3"], "m2_no_original": ["r2"], "idempotency_removed": ["r2"],
                 "authority_removed": ["r2"]}


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


def _family(r):
    if r["kind"] == "run":
        return "run"
    if r["kind"] == "cex":
        return "enforced counterexample" if r["control"] == "enforced" else "control counterexample"
    return None


def verify(doc, rows):
    res = doc["result"]
    g = EV.Gate("b1_modelcheck", doc)
    g.pinned({"depth": DEPTH, "ctl_depth": CTL_DEPTH})
    fam = g.split(rows, _family)
    g.family("run", fam.get("run", []), lambda r: ("run", r["arm"], r["scenario"], r["depth"]),
             {EV.key("run", "enforced", s, d) for s, d in DEPTH.items()} | {EV.key("run", n, s, CTL_DEPTH[s]) for n, scs in CTL_SCENARIOS.items()
                                                                          for s in scs})
    cexs = g.family("control counterexample", fam.get("control counterexample", []), lambda r: ("cex", r["control"]),
                    {EV.key("cex", n) for n in EXPECTED})
    if not g.ok:
        return g.fail(agrees=res.get("verdict") == "FAIL")
    checks = {"evidence": g.report()}
    runs = [r for r in fam["run"] if r["arm"] == "enforced"]
    codes = {}
    for r in runs:
        for k, v in r["codes"].items():
            codes[k] = codes.get(k, 0) + v
    states, trans = sum(r["states"] for r in runs), sum(r["transitions"] for r in runs)
    missing = sorted(set(APPLIED + REFUSED) - set(codes) - UNREACHABLE)
    enforced_cex = fam.get("enforced counterexample", [])
    checks["enforced_violations"] = sum(r["violations"] for r in runs)
    checks["enforced_cex_rows"] = len(enforced_cex)
    checks["states"], checks["transitions"], checks["codes_missing"] = states, trans, missing
    checks["contested_states"] = sum(r["contested_states"] for r in runs)
    ok = (checks["enforced_violations"] == 0 and not enforced_cex and states >= 1000 and trans >= 20000 and not missing
          and checks["contested_states"] > 0 and codes.get("resolved", 0) > 0)
    ctl = {}
    for name, inv in EXPECTED.items():
        r = cexs[EV.key("cex", name)]                 # exactly one row per control (the evidence gate)
        tr = r["trace"]
        shape = isinstance(tr, list) and tr and all(isinstance(e, dict) and all(k in e for k in COMMON) for e in tr) and tr[-1] == r["ev"]
        ctl[name] = {"rows": 1, "expected_recomputed": bool(shape) and violated(inv, r)}
    checks["controls"] = ctl
    ctl_ok = all(c["expected_recomputed"] for c in ctl.values())
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res["verdict"], "checks": checks}
