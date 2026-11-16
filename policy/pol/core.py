"""Decision core: a pure reducer over typed events. apply(state, event) -> (new_state, outcome). No clock, no randomness, no I/O:
the current-state table and its manifest hash are a function of the event log alone.

Event types: propose, remark (plain text: creates a PENDING change, never a decision), confirm (typed confirmation of a pending
change on an owner channel), decide (verb accept | reject | amend | resolve), supersede, retract, revoke, grant.
Contested is a state the reducer derives, not an event a client sends.

Rules (the invariants B1 checks in every reachable state):
  authority      only principals in authority[req] decide; revoked principals do nothing; authority is data in the state
                 and changes only through logged grant / revoke events
  self approval  an agent never accepts or amends a proposal authored by an agent (its own or a peer's)
  remark         creates a pending change; only a human on an owner channel, different from the remark's channel, confirms it
  binding        every decision names the head version it was made against; a decision against another version is refused
                 (stale) EXCEPT the one-step sibling window below
  contested      a decision bound to version b by a different principal, when the head moved from b by another principal's
                 decision with a different effect, is not refused and not last-writer-wins: the requirement becomes
                 `contested` (both candidates recorded, value withheld) until an authorised principal resolves it against
                 the contested head. The same effect is acknowledged as concurrent_agree and changes nothing.
  idempotency    one idem key = one effect; the same key and payload returns duplicate; the same key with another payload is refused
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

OWNER_CHANNELS = ("owner_ui", "owner_cli")
AGENT_CHANNELS = ("agent_api",)
DECISION_VERBS = ("accept", "reject", "amend", "resolve")
APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
REFUSED = ("bad_event", "unauthorized", "principal_revoked", "self_approval", "stale", "unknown_req", "unknown_proposal", "proposal_closed",
           "channel_mismatch", "agent_cannot_confirm", "same_channel_confirmation", "unknown_pending", "pending_closed", "idem_conflict",
           "nothing_to_retract", "not_contested", "contested_needs_resolve", "unknown_candidate")
OUTCOME_CODES = APPLIED + REFUSED + ("duplicate",)
STATE_CHANGING = ("confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted", "contested_opened", "resolved")

REQUIRED = {
    "propose": ("id", "idem", "req", "principal", "value", "based_on"),
    "remark": ("id", "idem", "req", "principal", "value", "channel", "based_on"),
    "confirm": ("id", "idem", "req", "principal", "pending", "channel", "based_on"),
    "decide": ("id", "idem", "req", "principal", "verb", "channel", "based_on"),
    "supersede": ("id", "idem", "req", "principal", "value", "channel", "based_on"),
    "retract": ("id", "idem", "req", "principal", "channel", "based_on"),
    "revoke": ("id", "idem", "principal", "target"),
    "grant": ("id", "idem", "principal", "req", "target"),
}


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def sha(o) -> str:
    return hashlib.sha256(canon(o).encode()).hexdigest()


def new_req():
    return {"version": 0, "status": "unset", "value": None, "value_event": None, "value_by": None, "value_version": 0, "chain": [], "log": [],
            "proposals": [], "pending": [], "contested": None, "last": None}


def genesis(principals: Dict[str, str], authority: Dict[str, List[str]], admins: List[str]) -> dict:
    return {"principals": dict(principals), "authority": {r: sorted(v) for r, v in authority.items()}, "admins": sorted(admins), "revoked": [],
            "idem": {}, "reqs": {r: new_req() for r in authority}}


def _kind(st, pid):
    return st["principals"].get(pid)


def _find(lst, key, val):
    for x in lst:
        if x["id"] == val:
            return x
    return None


def _push_chain(rq):
    if rq["value"] is not None:
        rq["chain"].append({"version": rq["value_version"], "value": rq["value"], "event": rq["value_event"], "by": rq["value_by"]})


def _set_value(rq, value, status, ev, by):
    _push_chain(rq)
    rq["value"], rq["status"], rq["value_event"], rq["value_by"] = value, status, ev, by
    rq["value_version"] = rq["version"] + 1
    rq["contested"] = None


def _effect(ev, rq):
    t = ev["type"]
    if t == "decide":
        v = ev["verb"]
        if v == "accept":
            p = _find(rq["proposals"], "id", ev.get("proposal"))
            return ["set", p["value"]] if p else ["set", None]
        if v == "reject":
            return ["reject", ev.get("proposal")]
        if v == "amend":
            return ["set", ev.get("value")]
    if t == "supersede":
        return ["set", ev["value"]]
    if t == "retract":
        return ["retract"]
    if t == "confirm":
        pc = _find(rq["pending"], "id", ev["pending"])
        return ["set", pc["value"]] if pc else ["set", None]
    return None


def apply(state: dict, ev: dict, flags: FrozenSet[str] = frozenset()) -> Tuple[dict, str]:
    """Pure. `flags` switch off individual rules: ONLY for the model-check controls."""
    if not isinstance(ev, dict) or ev.get("type") not in REQUIRED or not all(k in ev for k in REQUIRED[ev["type"]]):
        return state, "bad_event"
    if not all(isinstance(ev[k], str) and ev[k] for k in ("id", "idem")):
        return state, "bad_event"
    fp = sha(ev)
    if "idempotency" not in flags and ev["idem"] in state["idem"]:
        old = state["idem"][ev["idem"]]
        return state, ("duplicate" if old[0] == fp else "idem_conflict")
    try:
        ns, code = _apply(state, ev, flags)
    except Refuse as r:
        ns, code = state, str(r)
    if ns is state:
        ns = dict(state)
    ns["idem"] = dict(ns["idem"])
    ns["idem"][ev["idem"]] = [fp, code]
    return ns, code


class Refuse(Exception):
    pass


def _refuse(state, code):
    return state, code


def _apply(state, ev, flags):
    t, pid = ev["type"], ev["principal"]
    kind = _kind(state, pid)
    if kind is None:
        return _refuse(state, "unauthorized")
    if pid in state["revoked"]:
        return _refuse(state, "principal_revoked")
    if t == "revoke" or t == "grant":
        if pid not in state["admins"]:
            return _refuse(state, "unauthorized")
        ns = dict(state)
        if t == "revoke":
            if _kind(state, ev["target"]) is None:
                return _refuse(state, "bad_event")
            ns["revoked"] = sorted(set(state["revoked"]) | {ev["target"]})
            ns["reqs"] = {}
            for r, rq in state["reqs"].items():
                rq2 = copy.deepcopy(rq)
                for pc in rq2["pending"]:
                    if pc["author"] == ev["target"] and pc["status"] == "open":
                        pc["status"] = "invalidated"
                ns["reqs"][r] = rq2
            return ns, "revoked"
        if ev["req"] not in state["authority"] or _kind(state, ev["target"]) is None or ev["target"] in state["revoked"]:
            return _refuse(state, "bad_event")
        ns["authority"] = {r: list(v) for r, v in state["authority"].items()}
        ns["authority"][ev["req"]] = sorted(set(ns["authority"][ev["req"]]) | {ev["target"]})
        return ns, "granted"
    rid = ev["req"]
    if rid not in state["reqs"]:
        return _refuse(state, "unknown_req")
    ns = dict(state)
    ns["reqs"] = dict(state["reqs"])
    rq = copy.deepcopy(state["reqs"][rid])
    ns["reqs"][rid] = rq

    if t == "propose":
        rq["proposals"].append({"id": ev["id"], "value": ev["value"], "author": pid, "author_kind": kind, "status": "open", "base": ev["based_on"]})
        return ns, "proposed"
    if t == "remark":
        if "remark_is_decision" in flags:                        # CONTROL: a remark applies directly
            _set_value(rq, ev["value"], "current", ev["id"], pid)
            rq["version"] += 1
            rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": "remark", "based_on": ev["based_on"]})
            return ns, "confirmed_applied"
        rq["pending"].append({"id": ev["id"], "value": ev["value"], "author": pid, "channel": ev["channel"], "status": "open", "base": ev["based_on"]})
        return ns, "pending_created"

    # ---- everything below changes the head: authority first
    if "authority_check" not in flags and pid not in state["authority"][rid]:
        return _refuse(state, "unauthorized")
    chan = ev["channel"]
    if "channel_check" not in flags:
        if (kind == "human" and chan not in OWNER_CHANNELS) or (kind == "agent" and chan not in AGENT_CHANNELS):
            return _refuse(state, "channel_mismatch")
    if t == "confirm":
        if kind != "human":
            return _refuse(state, "agent_cannot_confirm")
        pc = _find(rq["pending"], "id", ev["pending"])
        if pc is None:
            return _refuse(state, "unknown_pending")
        if pc["status"] != "open":
            return _refuse(state, "pending_closed")
        if pc["channel"] == chan:
            return _refuse(state, "same_channel_confirmation")
    prop = None
    if t == "decide":
        verb = ev["verb"]
        if verb not in DECISION_VERBS:
            return _refuse(state, "bad_event")
        if verb in ("accept", "reject", "amend"):
            prop = _find(rq["proposals"], "id", ev.get("proposal"))
            if prop is None:
                return _refuse(state, "unknown_proposal")
            if "self_approval" not in flags and verb in ("accept", "amend") and prop["author_kind"] == "agent" and kind == "agent":
                return _refuse(state, "self_approval")
        if verb == "amend" and "value" not in ev:
            return _refuse(state, "bad_event")
    based = ev["based_on"]
    head = rq["version"]

    # ---- contested head: only a resolve bound to the contested head, or a sibling joining the window
    if rq["status"] == "contested" and "staleness" not in flags:
        c = rq["contested"]
        if t == "decide" and ev.get("verb") == "resolve":
            if based != head:
                return _refuse(state, "stale")
            return _resolve(ns, rq, ev, pid)
        if based == c["base"] and "lww" not in flags:
            eff = _effect(ev, rq)
            if eff is None:
                return _refuse(state, "bad_event")
            c["candidates"].append({"event": ev["id"], "by": pid, "effect": eff, "verb": ev.get("verb", t)})
            return ns, "contested_joined"
        if based == head:
            return _refuse(state, "contested_needs_resolve")
        return _refuse(state, "stale")
    if t == "decide" and ev.get("verb") == "resolve":
        return _refuse(state, "stale" if "staleness" not in flags and based != head else "not_contested")

    stale = based != head
    if stale and "staleness" not in flags:
        last = rq["last"]
        sib = (last is not None and based == last["based_on"] and head == based + 1 and last["by"] != pid)
        if not sib:
            return _refuse(state, "stale")
        if "lww" in flags:                                       # CONTROL: the later writer silently overwrites
            return _normal(ns, rq, ev, pid, kind, prop, flags)
        eff = _effect(ev, rq)
        if eff is None:
            return _refuse(state, "bad_event")
        if eff == last["effect"]:
            return state, "concurrent_agree"
        # conflicting concurrent decisions from different authorised principals: contested, value withheld
        prior = last["prev"]
        rq["contested"] = {"base": based, "prior": prior, "candidates": [{"event": last["event"], "by": last["by"], "effect": last["effect"], "verb": last["verb"]},
                                                                           {"event": ev["id"], "by": pid, "effect": eff, "verb": ev.get("verb", t)}]}
        rq["status"], rq["value"] = "contested", None
        rq["version"] += 1
        rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": "contest", "based_on": based})
        rq["last"] = None
        return ns, "contested_opened"
    return _normal(ns, rq, ev, pid, kind, prop, flags)


def _normal(ns, rq, ev, pid, kind, prop, flags):
    t = ev["type"]
    prev = {"value": rq["value"], "status": rq["status"], "value_event": rq["value_event"], "value_by": rq["value_by"], "value_version": rq["value_version"]}
    eff = _effect(ev, rq)
    code = None
    if t == "decide":
        v = ev["verb"]
        if prop["status"] != "open":
            raise Refuse("proposal_closed")
        if v == "accept":
            if eff[1] is None:
                raise Refuse("bad_event")
            prop["status"] = "accepted"
            _set_value(rq, eff[1], "current", ev["id"], pid)
            code = "decided_accept"
        elif v == "reject":
            prop["status"] = "rejected"
            code = "decided_reject"
        else:
            prop["status"] = "accepted"
            _set_value(rq, eff[1], "current", ev["id"], pid)
            code = "decided_amend"
    elif t == "supersede":
        _set_value(rq, ev["value"], "current", ev["id"], pid)
        code = "superseded"
    elif t == "retract":
        if rq["status"] in ("unset", "retracted"):
            raise Refuse("nothing_to_retract")
        _push_chain(rq)
        rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_version"] = None, "retracted", ev["id"], pid, rq["version"] + 1
        code = "retracted"
    elif t == "confirm":
        pc = _find(rq["pending"], "id", ev["pending"])
        pc["status"] = "confirmed"
        _set_value(rq, pc["value"], "current", ev["id"], pid)
        code = "confirmed_applied"
    rq["version"] += 1
    rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": ev.get("verb", t), "based_on": ev["based_on"]})
    rq["last"] = {"event": ev["id"], "by": pid, "based_on": ev["based_on"], "effect": eff, "verb": ev.get("verb", t), "prev": prev}
    return ns, code


def _resolve(ns, rq, ev, pid):
    c = rq["contested"]
    prev_state = c["prior"]
    if "choose" in ev and ev["choose"] is not None:
        cand = next((x for x in c["candidates"] if x["event"] == ev["choose"]), None)
        if cand is None:
            raise Refuse("unknown_candidate")
        eff = cand["effect"]
    elif "value" in ev:
        eff = ["set", ev["value"]]
    else:
        raise Refuse("bad_event")
    rq["contested"] = None
    rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_version"] = (prev_state["value"], prev_state["status"], prev_state["value_event"],
                                                                                          prev_state["value_by"], prev_state["value_version"])
    if eff[0] == "set":
        _set_value(rq, eff[1], "current", ev["id"], pid)
    elif eff[0] == "retract":
        _push_chain(rq)
        rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_version"] = None, "retracted", ev["id"], pid, rq["version"] + 1
    elif eff[0] == "reject":
        p = _find(rq["proposals"], "id", eff[1])
        if p:
            p["status"] = "rejected"
    rq["version"] += 1
    rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": "resolve", "based_on": ev["based_on"]})
    rq["last"] = {"event": ev["id"], "by": pid, "based_on": ev["based_on"], "effect": eff, "verb": "resolve", "prev": dict(prev_state)}
    return ns, "resolved"


# ------------------------------------------------------------------------------------------------ derived views
def current_table(state: dict) -> List[dict]:
    """The current-state table, derivable purely from the log."""
    rows = []
    for r in sorted(state["reqs"]):
        q = state["reqs"][r]
        rows.append({"req": r, "version": q["version"], "status": q["status"], "value": q["value"], "decision_event": q["value_event"], "decided_by": q["value_by"],
                     "contested": None if q["contested"] is None else sorted(c["event"] for c in q["contested"]["candidates"])})
    return rows


def manifest_hash(state: dict, log: List[dict]) -> str:
    """Hash over the current-state table, the authority map, the revoked set and the hash chain of the event log."""
    chain = "0"
    for e in log:
        chain = sha([chain, e])
    return sha({"table": current_table(state), "authority": state["authority"], "revoked": state["revoked"], "admins": state["admins"], "log_chain": chain})


def explain(state: dict, req: str) -> dict:
    """What is current for a requirement, and why: value, the deciding event, who decided, the chain of superseded values."""
    q = state["reqs"][req]
    out = {"req": req, "status": q["status"], "version": q["version"], "value": q["value"], "decision_event": q["value_event"], "decided_by": q["value_by"],
           "superseded_chain": list(q["chain"]), "open_proposals": [p["id"] for p in q["proposals"] if p["status"] == "open"],
           "pending_changes": [p["id"] for p in q["pending"] if p["status"] == "open"], "contested": None}
    if q["contested"] is not None:
        c = q["contested"]
        out["contested"] = {"base_version": c["base"], "last_uncontested_value": c["prior"]["value"], "candidates": [dict(x) for x in c["candidates"]]}
    return out
