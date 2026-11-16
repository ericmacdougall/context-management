"""Helpers shared by tests and experiments: default labels, proposal content hashes, tenant stamping."""
from .core import apply, sha
from .labels import Label

TENANT = "T1"
LAB = Label([TENANT]).to_dict()


def phash(prop):
    return sha([prop["id"], prop["value"], prop["author"]])


def prepare(state, ev):
    """Fill what a well-behaved client fills: tenant, a label where one is required, and the proposal/pending hash."""
    ev = dict(ev)
    ev.setdefault("tenant", TENANT)
    if ev.get("type") in ("propose", "remark", "supersede") or (ev.get("type") == "decide" and (ev.get("verb") == "amend" or (ev.get("verb") == "resolve" and "value" in ev))):
        if "label" not in ev:
            ev["label"] = dict(LAB)
    if ev.get("type") == "decide" and ev.get("proposal") and "proposal_hash" not in ev and ev.get("req") in state.get("reqs", {}):
        p = next((x for x in state["reqs"][ev["req"]]["proposals"] if x["id"] == ev["proposal"]), None)
        if p:
            ev["proposal_hash"] = phash(p)
    return ev


def run(state, events, flags=frozenset()):
    codes = []
    for e in events:
        state, c = apply(state, prepare(state, e), flags)
        codes.append(c)
    return state, codes
