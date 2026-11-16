"""Invariants of the decision core, written against pre-state, event, post-state and outcome code, independently of the reducer's
own branching. Each check returns a list of violated invariant names (empty = holds)."""
from __future__ import annotations

import json

from .core import MAX_CANDIDATES, AGENT_CHANNELS, DECISION_VERBS, OWNER_CHANNELS, STATE_CHANGING, canon, idem_key, sha

DECISIONLIKE = STATE_CHANGING + ("contested_joined", "concurrent_agree")
REFUSAL_LIKE = None


def _strip(st):
    d = dict(st)
    d.pop("idem", None)
    return canon(d)


def state_invariants(st):
    bad = []
    for r, q in st["reqs"].items():
        log = q["log"]
        if q["version"] != len(log) or [e["v"] for e in log] != list(range(1, len(log) + 1)):
            bad.append("I8_version_equals_head_transitions")
        applied_bases = [e["based_on"] for e in log if e["verb"] not in ("contest", "resolve")]
        if len(applied_bases) != len(set(applied_bases)):
            bad.append("I7_two_applied_decisions_same_base")
        if (q["status"] == "contested") != (q["contested"] is not None) or (q["status"] == "contested" and (q["value"] is not None or len(q["contested"]["candidates"]) < 2)):
            bad.append("I10_contested_state_coherent")
    for r, q in st["reqs"].items():
        if q["contested"] is not None and (len(q["contested"]["candidates"]) > MAX_CANDIDATES or any(st["principals"].get(c["by"]) != "human" for c in q["contested"]["candidates"])):
            bad.append("I14_contest_held_open_by_agent_or_unbounded")
        if len(q["ids"]) != len(set(q["ids"])):
            bad.append("I13_duplicate_event_id_applied")
    if json.loads(json.dumps(st, sort_keys=True)) != st:
        bad.append("I9_snapshot_roundtrip")
    return bad


def transition_invariants(pre, ev, post, code):
    bad = []
    t = ev.get("type")
    rid = ev.get("req")
    kind = pre["principals"].get(ev.get("principal"))
    if rid in pre["reqs"]:
        q0, q1 = pre["reqs"][rid], post["reqs"][rid]
        if code in DECISIONLIKE and (ev["principal"] not in pre["authority"][rid] or ev["principal"] in pre["revoked"]):
            bad.append("I1_authority")
        if kind != "human" and code in DECISIONLIKE:
            # an agent may only accept (a proposal a human authored) or reject; it never installs a value of its own
            prop = next((p for p in q0["proposals"] if p["id"] == ev.get("proposal")), None)
            allowed = t == "decide" and (ev.get("verb") == "reject" or (ev.get("verb") == "accept" and prop is not None and prop["author_kind"] == "human"))
            if not allowed:
                bad.append("I2_agent_head_move")
        if t in ("propose", "remark") and (q0["value"], q0["status"], q0["version"]) != (q1["value"], q1["status"], q1["version"]):
            bad.append("I3_remark_or_proposal_changed_value")
        if (q0["value"], q0["status"]) != (q1["value"], q1["status"]) and t not in ("confirm", "decide", "supersede", "retract"):
            bad.append("I3_value_changed_by_untyped_event")
        if code in ("decided_accept", "decided_reject", "decided_amend", "superseded", "retracted", "confirmed_applied", "resolved") and ev["based_on"] != q0["version"]:
            bad.append("I4_decision_not_bound_to_head")
        if code == "contested_opened" and (q0["last"] is None or ev["based_on"] != q0["last"]["based_on"]):
            bad.append("I4_decision_not_bound_to_head")
        if code == "contested_joined" and (q0["contested"] is None or ev["based_on"] != q0["contested"]["base"]):
            bad.append("I4_decision_not_bound_to_head")
        if not (q0["version"] <= q1["version"] <= q0["version"] + 1):
            bad.append("I8_version_monotonic")
        if code in DECISIONLIKE and t in ("decide", "supersede", "retract", "confirm") and (
                (kind == "human" and ev["channel"] not in OWNER_CHANNELS) or (kind != "human" and ev["channel"] not in AGENT_CHANNELS)):
            bad.append("I11_confirmation_not_typed_on_separate_owner_channel")
        if code == "confirmed_applied" and t == "confirm":
            pc = next((p for p in q0["pending"] if p["id"] == ev["pending"]), None)
            if kind != "human" or ev["channel"] not in OWNER_CHANNELS or (pc and pc["channel"] == ev["channel"]):
                bad.append("I11_confirmation_not_typed_on_separate_owner_channel")
        if code in ("decided_accept", "decided_amend"):
            pr = next((p for p in q0["proposals"] if p["id"] == ev.get("proposal")), None)
            if pr is None or pr["status"] != "open":
                bad.append("I12_value_from_closed_proposal")
        if code in ("contested_opened", "contested_joined") and ev.get("proposal"):
            pr = next((p for p in q0["proposals"] if p["id"] == ev["proposal"]), None)
            closers = {q0["last"]["event"]} if (code == "contested_opened" and q0["last"]) else {c["event"] for c in (q0["contested"] or {"candidates": []})["candidates"]}
            if pr is None or not (pr["status"] == "open" or pr.get("closed_by") in closers):
                bad.append("I12_value_from_closed_proposal")
        if code != "duplicate_id" and ev.get("id") in q0["ids"] and code in DECISIONLIKE + ("proposed", "pending_created"):
            bad.append("I13_duplicate_event_id_applied")
    refused = code not in DECISIONLIKE and code not in ("proposed", "pending_created", "revoked", "granted")
    if refused and _strip(pre) != _strip(post):
        bad.append("I5_refused_event_changed_state")
    ik = idem_key(ev) if isinstance(ev.get("idem"), str) and isinstance(ev.get("principal"), str) else None
    if ik in pre["idem"] and pre["idem"][ik][0] == sha(ev) and _strip(pre) != _strip(post):
        bad.append("I6_redelivery_changed_state")
    return bad
