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

from .labels import Label, join_dicts

KINDS = ("human", "agent", "analyst")          # closed set: anything not "human" gets the agent restrictions
MAX_CANDIDATES = 8

OWNER_CHANNELS = ("owner_ui", "owner_cli")
AGENT_CHANNELS = ("agent_api",)
DECISION_VERBS = ("accept", "reject", "amend", "resolve")
APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
REFUSED = ("bad_event", "unauthorized", "principal_revoked", "self_approval", "agent_not_permitted", "stale", "unknown_req", "unknown_proposal", "proposal_closed",
           "channel_mismatch", "agent_cannot_confirm", "same_channel_confirmation", "unknown_pending", "pending_closed", "idem_conflict",
           "nothing_to_retract", "not_contested", "contested_needs_resolve", "unknown_candidate", "tenant_mismatch", "duplicate_id", "unlabelled", "proposal_mismatch", "candidates_full")
OUTCOME_CODES = APPLIED + REFUSED + ("duplicate",)
STATE_CHANGING = ("confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted", "contested_opened", "resolved")

REQUIRED = {
    "propose": ("id", "idem", "tenant", "req", "principal", "value", "based_on"),
    "remark": ("id", "idem", "tenant", "req", "principal", "value", "channel", "based_on"),
    "confirm": ("id", "idem", "tenant", "req", "principal", "pending", "channel", "based_on"),
    "decide": ("id", "idem", "tenant", "req", "principal", "verb", "channel", "based_on"),
    "supersede": ("id", "idem", "tenant", "req", "principal", "value", "channel", "based_on"),
    "retract": ("id", "idem", "tenant", "req", "principal", "channel", "based_on"),
    "revoke": ("id", "idem", "tenant", "principal", "target"),
    "grant": ("id", "idem", "tenant", "principal", "req", "target"),
}


def idem_key(ev) -> str:
    """Idempotency keys are scoped to the principal: one principal cannot burn another's key."""
    return "%s\x1f%s" % (ev["principal"], ev["idem"])


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def sha(o) -> str:
    return hashlib.sha256(canon(o).encode()).hexdigest()


def new_req():
    return {"version": 0, "status": "unset", "value": None, "value_label": None, "value_event": None, "value_by": None, "value_version": 0, "chain": [], "log": [],
            "proposals": [], "pending": [], "contested": None, "last": None, "ids": []}


def genesis(tenant: str, principals: Dict[str, str], authority: Dict[str, List[str]], admins: List[str], req_labels: Optional[Dict[str, dict]] = None) -> dict:
    """One state per tenant: authority, admins, revocations and requirement labels are tenant data."""
    if not isinstance(tenant, str) or not tenant or any(k not in KINDS for k in principals.values()):
        raise ValueError("tenant must be a non-empty string and principal kinds must be one of %s" % (KINDS,))
    base = Label([tenant]).to_dict()
    return {"tenant": tenant, "principals": dict(principals), "authority": {r: sorted(v) for r, v in authority.items()}, "admins": sorted(admins), "revoked": [],
            "idem": {}, "reqs": {r: new_req() for r in authority}, "req_labels": {r: dict((req_labels or {}).get(r, base)) for r in authority}}


def _kind(st, pid):
    return st["principals"].get(pid)


def _find(lst, key, val):
    for x in lst:
        if x["id"] == val:
            return x
    return None


def _push_chain(rq):
    if rq["value"] is not None:
        rq["chain"].append({"version": rq["value_version"], "value": rq["value"], "event": rq["value_event"], "by": rq["value_by"], "label": rq["value_label"]})


def _set_value(rq, value, status, ev, by, label=None):
    _push_chain(rq)
    rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_label"] = value, status, ev, by, label
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


def _eff_label(ev, rq, rl):
    """COMPUTED label of the value a decision would install: the join of the requirement's label, the labels of the content it adopts and
    the event's own label. An event can add restriction, never remove it."""
    t = ev["type"]
    own = ev.get("label")
    if t == "decide" and ev.get("verb") in ("accept", "amend"):
        p = _find(rq["proposals"], "id", ev.get("proposal"))
        if p is None:
            return None
        return join_dicts(rl, p["label"], own) if ev["verb"] == "amend" else join_dicts(rl, p["label"])
    if t == "confirm":
        pc = _find(rq["pending"], "id", ev["pending"])
        return join_dicts(rl, pc["label"], own) if pc else None
    if own is None:
        return None
    return join_dicts(rl, own)


def apply(state: dict, ev: dict, flags: FrozenSet[str] = frozenset()) -> Tuple[dict, str]:
    """Pure. `flags` switch off individual rules: ONLY for the model-check controls."""
    if not isinstance(ev, dict) or ev.get("type") not in REQUIRED or not all(k in ev for k in REQUIRED[ev["type"]]):
        return state, "bad_event"
    if not all(isinstance(ev[k], str) and ev[k] for k in ("id", "idem", "principal")):
        return state, "bad_event"
    if ev["tenant"] != state["tenant"]:
        return state, "tenant_mismatch"
    if "label" in ev and ev["label"] is not None:
        try:
            Label.from_dict(ev["label"])
        except Exception:       # noqa: BLE001
            return state, "bad_event"
    fp = sha(ev)
    ik = idem_key(ev)
    if "idempotency" not in flags and ik in state["idem"]:
        old = state["idem"][ik]
        return state, ("duplicate" if old[0] == fp else "idem_conflict")
    try:
        ns, code = _apply(state, ev, flags)
    except Refuse as r:
        ns, code = state, str(r)
    if code in REFUSED:
        return state, code                     # a refused request does not burn its idempotency key and changes nothing
    if ns is state:
        ns = dict(state)
    ns["idem"] = dict(ns["idem"])
    ns["idem"][ik] = [fp, code]
    return ns, code


def apply_ex(state: dict, ev: dict, flags: FrozenSet[str] = frozenset()) -> Tuple[dict, str, Optional[str]]:
    """Like apply, but a duplicate also returns the ORIGINAL outcome code of the first delivery."""
    ns, code = apply(state, ev, flags)
    orig = None
    if code in ("duplicate", "idem_conflict"):
        orig = state["idem"][idem_key(ev)][1]
    return ns, code, orig


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
    rl = state["req_labels"][rid]
    if "unique_ids" not in flags and ev["id"] in rq["ids"]:
        return _refuse(state, "duplicate_id")
    needs_label = t in ("propose", "remark", "supersede") or (t == "decide" and ev.get("verb") in ("amend",) ) or (t == "decide" and ev.get("verb") == "resolve" and "value" in ev)
    if needs_label and ev.get("label") is None:
        return _refuse(state, "unlabelled")
    rq["ids"].append(ev["id"])

    if t == "propose":
        rq["proposals"].append({"id": ev["id"], "value": ev["value"], "author": pid, "author_kind": kind, "status": "open", "base": ev["based_on"], "label": ev.get("label")})
        return ns, "proposed"
    if t == "remark":
        if "remark_is_decision" in flags:                        # CONTROL: a remark applies directly
            _set_value(rq, ev["value"], "current", ev["id"], pid, ev.get("label"))
            rq["version"] += 1
            rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": "remark", "based_on": ev["based_on"]})
            return ns, "confirmed_applied"
        rq["pending"].append({"id": ev["id"], "value": ev["value"], "author": pid, "channel": ev["channel"], "status": "open", "base": ev["based_on"], "label": ev.get("label")})
        return ns, "pending_created"

    # ---- everything below changes the head: authority first
    if "authority_check" not in flags and pid not in state["authority"][rid]:
        return _refuse(state, "unauthorized")
    chan = ev["channel"]
    if "channel_check" not in flags:
        if (kind == "human" and chan not in OWNER_CHANNELS) or (kind != "human" and chan not in AGENT_CHANNELS):
            return _refuse(state, "channel_mismatch")
    if kind != "human" and "self_approval" not in flags and (t in ("supersede", "retract") or (t == "decide" and ev["verb"] in ("amend", "resolve"))):
        return _refuse(state, "agent_not_permitted")      # an agent may only accept or reject proposals; it never installs a value of its own
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
            if "self_approval" not in flags and verb == "accept" and prop["author_kind"] != "human" and kind != "human":
                return _refuse(state, "self_approval")
        if prop is not None:
            if ev.get("proposal_hash") is None:
                return _refuse(state, "bad_event")
            if ev["proposal_hash"] != sha([prop["id"], prop["value"], prop["author"]]):
                return _refuse(state, "proposal_mismatch")     # an accept names the exact proposal by id AND content hash
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
            return _resolve(ns, rq, ev, pid, rl)
        if based == c["base"] and "lww" not in flags:
            eff = _effect(ev, rq)
            if eff is None:
                return _refuse(state, "bad_event")
            if "agent_contest" not in flags and (kind != "human" or any(_kind(state, x["by"]) != "human" for x in c["candidates"])):
                return _refuse(state, "stale")                 # an agent never joins or keeps a contest open
            if len(c["candidates"]) >= MAX_CANDIDATES:
                return _refuse(state, "candidates_full")
            if "source_open" not in flags and not _source_open(rq, ev, {x["event"] for x in c["candidates"]}):
                return _refuse(state, "proposal_closed")
            c["candidates"].append({"event": ev["id"], "by": pid, "effect": eff, "verb": ev.get("verb", t), "label": _eff_label(ev, rq, rl)})
            return ns, "contested_joined"
        if based == head:
            return _refuse(state, "contested_needs_resolve")
        return _refuse(state, "stale")
    if t == "decide" and ev.get("verb") == "resolve":
        return _refuse(state, "stale" if "staleness" not in flags and based != head else "not_contested")

    stale = based != head
    if stale and "staleness" not in flags:
        last = rq["last"]
        sib = (last is not None and based == last["based_on"] and head == based + 1 and last["by"] != pid and last["verb"] != "resolve"
               and ("agent_contest" in flags or (kind == "human" and _kind(state, last["by"]) == "human")))        # humans only; never against a resolve
        if sib and "source_open" not in flags and not _source_open(rq, ev, {last["event"]}):
            return _refuse(state, "proposal_closed")
        if not sib:
            return _refuse(state, "stale")
        if "lww" in flags:                                       # CONTROL: the later writer silently overwrites
            return _normal(ns, rq, ev, pid, kind, prop, flags, rl)
        eff = _effect(ev, rq)
        if eff is None:
            return _refuse(state, "bad_event")
        if eff == last["effect"]:
            return state, "concurrent_agree"
        # conflicting concurrent decisions from different authorised principals: contested, value withheld
        prior = last["prev"]
        rq["contested"] = {"base": based, "prior": prior, "candidates": [{"event": last["event"], "by": last["by"], "effect": last["effect"], "verb": last["verb"], "label": last.get("label")},
                                                                           {"event": ev["id"], "by": pid, "effect": eff, "verb": ev.get("verb", t), "label": _eff_label(ev, rq, rl)}]}
        rq["status"], rq["value"] = "contested", None
        rq["version"] += 1
        rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": "contest", "based_on": based})
        rq["last"] = None
        return ns, "contested_opened"
    return _normal(ns, rq, ev, pid, kind, prop, flags, rl)


def _source_open(rq, ev, closers):
    """A sibling or candidate may only adopt content that is still open, or was closed by the very decision it competes with."""
    if ev["type"] == "decide" and ev.get("verb") in ("accept", "reject", "amend"):
        p = _find(rq["proposals"], "id", ev.get("proposal"))
        return p is not None and (p["status"] == "open" or p.get("closed_by") in closers)
    if ev["type"] == "confirm":
        p = _find(rq["pending"], "id", ev["pending"])
        return p is not None and (p["status"] == "open" or p.get("closed_by") in closers)
    return True


def _normal(ns, rq, ev, pid, kind, prop, flags, rl):
    t = ev["type"]
    prev = {"value": rq["value"], "status": rq["status"], "value_event": rq["value_event"], "value_by": rq["value_by"], "value_version": rq["value_version"],
            "value_label": rq["value_label"]}
    eff = _effect(ev, rq)
    eff_label = _eff_label(ev, rq, rl)
    code = None
    if t == "decide":
        v = ev["verb"]
        if prop["status"] != "open":
            raise Refuse("proposal_closed")
        if v == "accept":
            if eff[1] is None:
                raise Refuse("bad_event")
            prop["status"] = "accepted"
            prop["closed_by"] = ev["id"]
            _set_value(rq, eff[1], "current", ev["id"], pid, eff_label)
            code = "decided_accept"
        elif v == "reject":
            prop["status"] = "rejected"
            prop["closed_by"] = ev["id"]
            code = "decided_reject"
        else:
            prop["status"] = "accepted"
            prop["closed_by"] = ev["id"]
            _set_value(rq, eff[1], "current", ev["id"], pid, eff_label)
            code = "decided_amend"
    elif t == "supersede":
        _set_value(rq, ev["value"], "current", ev["id"], pid, eff_label)
        code = "superseded"
    elif t == "retract":
        if rq["status"] in ("unset", "retracted"):
            raise Refuse("nothing_to_retract")
        _push_chain(rq)
        rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_version"], rq["value_label"] = None, "retracted", ev["id"], pid, rq["version"] + 1, None
        code = "retracted"
    elif t == "confirm":
        pc = _find(rq["pending"], "id", ev["pending"])
        pc["status"] = "confirmed"
        pc["closed_by"] = ev["id"]
        _set_value(rq, pc["value"], "current", ev["id"], pid, eff_label)
        code = "confirmed_applied"
    rq["version"] += 1
    rq["log"].append({"v": rq["version"], "event": ev["id"], "by": pid, "verb": ev.get("verb", t), "based_on": ev["based_on"]})
    rq["last"] = {"event": ev["id"], "by": pid, "based_on": ev["based_on"], "effect": eff, "verb": ev.get("verb", t), "prev": prev, "label": eff_label}
    return ns, code


def _resolve(ns, rq, ev, pid, rl):
    c = rq["contested"]
    prev_state = c["prior"]
    if "choose" in ev and ev["choose"] is not None:
        cand = next((x for x in c["candidates"] if x["event"] == ev["choose"]), None)
        if cand is None:
            raise Refuse("unknown_candidate")
        eff = cand["effect"]
        cand_label = cand.get("label")
    elif "value" in ev:
        eff = ["set", ev["value"]]
        cand_label = join_dicts(rl, ev["label"]) if ev.get("label") is not None else None
        if cand_label is None:
            raise Refuse("unlabelled")
    else:
        raise Refuse("bad_event")
    rq["contested"] = None
    rq["value"], rq["status"], rq["value_event"], rq["value_by"], rq["value_version"], rq["value_label"] = (
        prev_state["value"], prev_state["status"], prev_state["value_event"], prev_state["value_by"], prev_state["value_version"], prev_state.get("value_label"))
    if eff[0] == "set":
        _set_value(rq, eff[1], "current", ev["id"], pid, cand_label)
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
