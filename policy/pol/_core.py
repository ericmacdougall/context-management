"""Decision core reducer (PRIVATE). The rules are CORE-RULES.md; this is the production implementation.

reduce(view, ev) -> (code, original_code, delta). The reducer READS through a small view interface (MemoryState here, the Postgres
engine in pgcore) and RETURNS the changes as a delta; it never mutates. A refused code has an empty delta. Every read is a point lookup
(head row, one proposal, one id, one idempotency key, two counters), and every delta op touches a bounded number of rows, so the cost of
an event does not grow with history (N2). There are no switches: broken variants for controls are built by the experiment harness as
one-patch source variants of this module (exp/patches.py)."""
from __future__ import annotations

import copy
import hashlib
import json

from .labels import Label, LabelError, join

KINDS = ("human", "agent", "analyst")
OWNER_CHANNELS = ("owner_ui", "owner_cli")
AGENT_CHANNELS = ("agent_api",)
VERBS = ("accept", "reject", "amend", "resolve")
MAX_CANDIDATES = 8
MAX_OPEN_PER_PRINCIPAL = 16
MAX_OPEN_PER_REQ = 256          # open items authored by NON-HUMANS on one req; binds non-human authors only (R3-4)

REFUSED = ("bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap",
           "channel_mismatch", "agent_not_permitted", "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation",
           "unknown_proposal", "proposal_mismatch", "stale", "proposal_closed", "candidates_full", "contested_needs_resolve",
           "not_contested", "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")
OUTCOME_CODES = APPLIED + REFUSED

COMMON = ("type", "id", "idem", "tenant", "principal", "channel")
REQUIRED = {"propose": ("req", "value_ref", "label", "based_on"), "remark": ("req", "value_ref", "label", "based_on"),
            "confirm": ("req", "pending", "based_on"), "decide": ("req", "verb", "based_on"), "supersede": ("req", "value_ref", "label", "based_on"),
            "retract": ("req", "based_on"), "revoke": ("target",), "grant": ("req", "target", "role")}
STR_FIELDS = ("id", "idem", "tenant", "principal", "channel", "req", "value_ref", "proposal", "proposal_hash", "pending", "choose", "target", "verb", "role")


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def sha(o) -> str:
    return hashlib.sha256(canon(o).encode()).hexdigest()


def proposal_hash(pid, value_ref, author) -> str:
    return sha([pid, value_ref, author])


def default_label(tenant):
    return Label([tenant]).to_dict()


def _jl(*dicts):
    return join(Label.from_dict(d) for d in dicts).to_dict()


def new_head():
    return {"version": 0, "status": "unset", "value_ref": None, "value_label": None, "value_event": None, "value_by": None, "value_version": 0,
            "last": None, "contested": None}


def _shape(ev):
    if not isinstance(ev, dict) or ev.get("type") not in REQUIRED:
        return False
    try:
        canon(ev)
    except (TypeError, ValueError):
        return False
    t = ev["type"]
    need = list(COMMON) + list(REQUIRED[t])
    if t == "decide":
        v = ev.get("verb")
        if v in ("accept", "reject"):
            need += ["proposal", "proposal_hash"]
        elif v == "amend":
            need += ["proposal", "proposal_hash", "value_ref", "label"]
        elif v == "resolve" and "choose" not in ev:
            need += ["value_ref", "label"]
    if any(k not in ev for k in need):
        return False
    for k in STR_FIELDS:
        if k in ev and not (isinstance(ev[k], str) and ev[k]):
            return False
    if "based_on" in ev and (not isinstance(ev["based_on"], int) or isinstance(ev["based_on"], bool) or ev["based_on"] < 0):
        return False
    if t == "grant" and ev["role"] not in ("decide", "propose"):
        return False
    if "label" in ev:
        try:
            Label.from_dict(ev["label"])
        except (LabelError, TypeError, ValueError, AttributeError):
            return False
    return True


def reduce(view, ev):
    """(code, original_code, delta)."""
    if not _shape(ev):
        return "bad_event", None, []
    if ev["tenant"] != view.tenant:
        return "tenant_mismatch", None, []
    pid = ev["principal"]
    fp = sha(ev)
    old = view.idem(pid, ev["idem"])
    if old is not None:
        return ("duplicate" if old[0] == fp else "idem_conflict"), old[1], []
    code, delta = _rules(view, ev)
    if code in REFUSED:
        return code, None, []
    delta.append(("idem", pid, ev["idem"], fp, code))
    if "req" in ev and ev["type"] != "revoke":
        delta.append(("id", ev["req"], ev["id"]))
    return code, None, delta


def _rules(view, ev):
    t, pid = ev["type"], ev["principal"]
    kind = view.kind(pid)
    if kind is None:
        return "unauthorized", []
    if view.is_revoked(pid):
        return "principal_revoked", []
    if t == "revoke":
        if not view.is_admin(pid):
            return "unauthorized", []
        if view.kind(ev["target"]) is None:
            return "bad_event", []
        return "revoked", [("revoke", ev["target"])]
    if t == "grant":
        if not view.is_admin(pid):
            return "unauthorized", []
        if not view.req_exists(ev["req"]) or view.kind(ev["target"]) is None or view.is_revoked(ev["target"]):
            return "bad_event", []
        return "granted", [("grant", ev["req"], ev["role"], ev["target"])]
    req = ev["req"]
    if not view.req_exists(req):
        return "unknown_req", []
    if view.id_used(req, ev["id"]):
        return "duplicate_id", []
    if t in ("propose", "remark"):
        if not (view.has_authority(req, pid) or view.is_proposer(req, pid)):
            return "unauthorized", []
        if view.open_count(req, pid) >= MAX_OPEN_PER_PRINCIPAL or (kind != "human" and view.open_nonhuman(req) >= MAX_OPEN_PER_REQ):
            return "proposal_cap", []
        doc = {"id": ev["id"], "kind": "proposal" if t == "propose" else "pending", "author": pid, "author_kind": kind, "value_ref": ev["value_ref"],
               "label": Label.from_dict(ev["label"]).to_dict(), "status": "open", "closed_by": None, "base": ev["based_on"],
               "channel": ev["channel"] if t == "remark" else None}
        return ("proposed" if t == "propose" else "pending_created"), [("prop_new", req, doc)]

    # ---- head-moving types
    if not view.has_authority(req, pid):
        return "unauthorized", []
    human = kind == "human"
    chan = ev["channel"]
    if (human and chan not in OWNER_CHANNELS) or (not human and chan not in AGENT_CHANNELS):
        return "channel_mismatch", []
    verb = ev.get("verb") if t == "decide" else None
    if not human:                                   # round 3 (ASSUMPTION): non-humans only propose and remark
        return ("agent_cannot_confirm" if t == "confirm" else "agent_not_permitted"), []
    pend = prop = None
    if t == "confirm":
        pend = view.prop(req, ev["pending"])
        if pend is None or pend["kind"] != "pending":
            return "unknown_pending", []
        if pend["status"] != "open":
            return "pending_closed", []
        if pend["channel"] == chan:
            return "same_channel_confirmation", []
    if t == "decide":
        if verb not in VERBS:
            return "bad_event", []
        if verb in ("accept", "reject", "amend"):
            prop = view.prop(req, ev["proposal"])
            if prop is None or prop["kind"] != "proposal":
                return "unknown_proposal", []
            if ev["proposal_hash"] != proposal_hash(prop["id"], prop["value_ref"], prop["author"]):
                return "proposal_mismatch", []
    h = copy.deepcopy(view.head(req))
    based = ev["based_on"]
    rl = view.req_label(req)

    eff = _effect(ev, prop, pend)
    elab = _eff_label(ev, prop, pend, rl)

    if h["status"] == "contested":
        c = h["contested"]
        if verb == "resolve":
            if based != h["version"]:
                return "stale", []
            return _resolve(view, req, h, ev, rl)
        if based == c["base"]:
            if len(c["candidates"]) >= MAX_CANDIDATES:
                return "candidates_full", []
            if not _adopted_open(prop, pend, {x["event"] for x in c["candidates"]}):
                return "proposal_closed", []
            c["candidates"].append({"event": ev["id"], "by": pid, "effect": eff, "verb": verb or t, "label": elab})
            return "contested_joined", [("head", req, h)]
        if based == h["version"]:
            return "contested_needs_resolve", []
        return "stale", []
    if verb == "resolve":
        return ("stale" if based != h["version"] else "not_contested"), []

    if based != h["version"]:
        last = h["last"]
        sib = (last is not None and based == last["based_on"] and h["version"] == based + 1 and last["by"] != pid and last["verb"] != "resolve")
        if not sib:
            return "stale", []
        if not _adopted_open(prop, pend, {last["event"]}):
            return "proposal_closed", []
        if eff == last["effect"]:
            return "concurrent_agree", []
        h["contested"] = {"base": based, "prior": last["prev"],
                          "candidates": [{"event": last["event"], "by": last["by"], "effect": last["effect"], "verb": last["verb"], "label": last["label"]},
                                         {"event": ev["id"], "by": pid, "effect": eff, "verb": verb or t, "label": elab}]}
        h["status"], h["value_ref"] = "contested", None
        h["version"] += 1
        h["last"] = None
        return "contested_opened", [("head", req, h)]

    # rule 15: normal application at the head
    delta = []
    prev = _prev(h)
    if t == "decide":
        if prop["status"] != "open":
            return "proposal_closed", []
        if verb == "reject":
            code = "decided_reject"
            delta.append(("prop_close", req, prop["id"], "rejected", ev["id"]))
        else:
            code = "decided_accept" if verb == "accept" else "decided_amend"
            delta.append(("prop_close", req, prop["id"], "accepted", ev["id"]))
            _set(h, eff[1], elab, ev, pid, delta, req)
    elif t == "supersede":
        code = "superseded"
        _set(h, eff[1], elab, ev, pid, delta, req)
    elif t == "retract":
        if h["status"] in ("unset", "retracted"):
            return "nothing_to_retract", []
        code = "retracted"
        _retract(h, ev, pid, delta, req)
    else:
        code = "confirmed_applied"
        delta.append(("prop_close", req, pend["id"], "confirmed", ev["id"]))
        _set(h, eff[1], elab, ev, pid, delta, req)
    h["version"] += 1
    h["last"] = {"event": ev["id"], "by": pid, "based_on": based, "effect": eff, "verb": verb or t, "prev": prev, "label": elab}
    delta.append(("head", req, h))
    return code, delta


def _prev(h):
    return {k: h[k] for k in ("value_ref", "status", "value_event", "value_by", "value_version", "value_label")}


def _effect(ev, prop, pend):
    t = ev["type"]
    if t == "decide":
        v = ev["verb"]
        if v == "accept":
            return ["set", prop["value_ref"]]
        if v == "reject":
            return ["reject", prop["id"]]
        if v == "amend":
            return ["set", ev["value_ref"]]
        return None
    if t == "supersede":
        return ["set", ev["value_ref"]]
    if t == "retract":
        return ["retract"]
    if t == "confirm":
        return ["set", pend["value_ref"]]
    return None


def _eff_label(ev, prop, pend, rl):
    t = ev["type"]
    if t == "decide" and ev["verb"] == "accept":
        return _jl(rl, prop["label"])
    if t == "decide" and ev["verb"] == "amend":
        return _jl(rl, prop["label"], ev["label"])
    if t == "confirm":
        return _jl(rl, pend["label"])
    if t == "supersede":
        return _jl(rl, ev["label"])
    return None


def _adopted_open(prop, pend, closers):
    c = prop if prop is not None else pend
    return c is None or c["status"] == "open" or c.get("closed_by") in closers


def _set(h, ref, label, ev, pid, delta, req):
    if h["value_ref"] is not None:
        delta.append(("chain", req, {"value_ref": h["value_ref"], "value_version": h["value_version"], "value_event": h["value_event"],
                                      "value_by": h["value_by"], "value_label": h["value_label"]}))
    h.update(value_ref=ref, status="current", value_event=ev["id"], value_by=pid, value_label=label, value_version=h["version"] + 1, contested=None)


def _retract(h, ev, pid, delta, req):
    if h["value_ref"] is not None:
        delta.append(("chain", req, {"value_ref": h["value_ref"], "value_version": h["value_version"], "value_event": h["value_event"],
                                      "value_by": h["value_by"], "value_label": h["value_label"]}))
    h.update(value_ref=None, status="retracted", value_event=ev["id"], value_by=pid, value_label=None, value_version=h["version"] + 1, contested=None)


def _resolve(view, req, h, ev, rl):
    c = h["contested"]
    pid = ev["principal"]
    if "choose" in ev:
        cand = next((x for x in c["candidates"] if x["event"] == ev["choose"]), None)
        if cand is None:
            return "unknown_candidate", []
        eff, lab = cand["effect"], cand["label"]
    else:
        eff, lab = ["set", ev["value_ref"]], _jl(rl, ev["label"])
    prior = c["prior"]
    h.update(prior)
    h["contested"] = None
    delta = []
    if eff[0] == "set":
        _set(h, eff[1], lab, ev, pid, delta, req)
    elif eff[0] == "retract":
        _retract(h, ev, pid, delta, req)
        lab = None
    else:
        delta.append(("prop_close", req, eff[1], "rejected", ev["id"]))
        lab = None
    h["version"] += 1
    h["last"] = {"event": ev["id"], "by": pid, "based_on": ev["based_on"], "effect": eff, "verb": "resolve", "prev": dict(prior), "label": lab}
    delta.append(("head", req, h))
    return "resolved", delta


# ------------------------------------------------------------------------------------------------ in-memory state (the view + delta)
class MemoryState:
    """Per-tenant state with O(1) point reads and O(1) delta ops (revoke: O(open items of the revoked principal))."""

    def __init__(self, config):
        t = config["tenant"]
        if not isinstance(t, str) or not t or any(k not in KINDS for k in config["principals"].values()):
            raise ValueError("tenant must be a non-empty str and principal kinds in %s" % (KINDS,))
        self.tenant = t
        self.principals = dict(config["principals"])
        self.authority = {r: set(v) for r, v in config["authority"].items()}
        self.proposers = {r: set(config.get("proposers", {}).get(r, ())) for r in config["authority"]}
        self.admins = set(config["admins"])
        self.revoked = set()
        self.req_labels = {r: Label.from_dict(config.get("req_labels", {}).get(r, default_label(t))).to_dict() for r in config["authority"]}
        self.heads = {r: new_head() for r in config["authority"]}
        self.props = {r: {} for r in config["authority"]}
        self.open_by = {}                 # (req, principal) -> set of open item ids
        self.open_n = {r: 0 for r in config["authority"]}           # open items with a NON-HUMAN author, per req
        self.ids = {r: set() for r in config["authority"]}
        self.idems = {}
        self.chains = {r: [] for r in config["authority"]}

    # view
    def kind(self, pid):
        return self.principals.get(pid)

    def is_revoked(self, pid):
        return pid in self.revoked

    def is_admin(self, pid):
        return pid in self.admins

    def has_authority(self, req, pid):
        return pid in self.authority.get(req, ())

    def is_proposer(self, req, pid):
        return pid in self.proposers.get(req, ())

    def req_exists(self, req):
        return req in self.heads

    def req_label(self, req):
        return self.req_labels[req]

    def head(self, req):
        return self.heads[req]

    def prop(self, req, iid):
        return self.props[req].get(iid)

    def id_used(self, req, iid):
        return iid in self.ids[req]

    def open_count(self, req, pid):
        return len(self.open_by.get((req, pid), ()))

    def open_nonhuman(self, req):
        return self.open_n[req]

    def idem(self, pid, key):
        return self.idems.get((pid, key))

    # delta
    def apply_delta(self, delta):
        for op in delta:
            k = op[0]
            if k == "head":
                self.heads[op[1]] = op[2]
            elif k == "prop_new":
                req, doc = op[1], op[2]
                self.props[req][doc["id"]] = doc
                self.open_by.setdefault((req, doc["author"]), set()).add(doc["id"])
                self.open_n[req] += doc["author_kind"] != "human"
            elif k == "prop_close":
                req, iid, status, by = op[1:]
                d = self.props[req][iid]
                if d["status"] == "open":
                    self.open_by[(req, d["author"])].discard(iid)
                    self.open_n[req] -= d["author_kind"] != "human"
                self.props[req][iid] = dict(d, status=status, closed_by=by)
            elif k == "chain":
                self.chains[op[1]].append(op[2])
            elif k == "idem":
                self.idems[(op[1], op[2])] = [op[3], op[4]]
            elif k == "id":
                self.ids[op[1]].add(op[2])
            elif k == "revoke":
                tgt = op[1]
                self.revoked.add(tgt)
                for (req, author), ids in list(self.open_by.items()):
                    if author != tgt:
                        continue
                    for iid in list(ids):
                        self.open_n[req] -= self.props[req][iid]["author_kind"] != "human"
                        self.props[req][iid] = dict(self.props[req][iid], status="invalidated", closed_by=None)
                    ids.clear()
            elif k == "grant":
                req, role, tgt = op[1:]
                (self.authority if role == "decide" else self.proposers)[req].add(tgt)
            else:
                raise AssertionError(k)

    def comparable(self):
        """The comparable view of CORE-RULES.md section 8."""
        return {"revoked": sorted(self.revoked), "authority": {r: sorted(v) for r, v in self.authority.items()},
                "proposers": {r: sorted(v) for r, v in self.proposers.items()},
                "reqs": {r: {"version": h["version"], "status": h["status"], "value_ref": h["value_ref"], "value_event": h["value_event"], "value_by": h["value_by"],
                             "value_version": h["value_version"], "value_label": h["value_label"],
                             "contested": None if h["contested"] is None else {"base": h["contested"]["base"],
                                                                               "candidates": [[c["event"], c["by"], c["effect"], c["verb"]] for c in h["contested"]["candidates"]]}}
                         for r, h in self.heads.items()},
                "proposals": {r: {i: d["status"] for i, d in p.items() if d["kind"] == "proposal"} for r, p in self.props.items()},
                "pending": {r: {i: d["status"] for i, d in p.items() if d["kind"] == "pending"} for r, p in self.props.items()}}

    def snapshot(self):
        """Deterministic JSON-able form of the whole state (for model checking, hashing and snapshot/restore)."""
        return {"tenant": self.tenant, "principals": self.principals, "authority": {r: sorted(v) for r, v in self.authority.items()},
                "proposers": {r: sorted(v) for r, v in self.proposers.items()}, "admins": sorted(self.admins), "revoked": sorted(self.revoked),
                "req_labels": self.req_labels, "heads": self.heads, "props": self.props, "ids": {r: sorted(v) for r, v in self.ids.items()},
                "idems": sorted([list(k) + v for k, v in self.idems.items()]), "chains": self.chains}

    @classmethod
    def restore(cls, snap):
        s = cls.__new__(cls)
        s.tenant = snap["tenant"]
        s.principals = dict(snap["principals"])
        s.authority = {r: set(v) for r, v in snap["authority"].items()}
        s.proposers = {r: set(v) for r, v in snap["proposers"].items()}
        s.admins, s.revoked = set(snap["admins"]), set(snap["revoked"])
        s.req_labels = copy.deepcopy(snap["req_labels"])
        s.heads = copy.deepcopy(snap["heads"])
        s.props = copy.deepcopy(snap["props"])
        s.ids = {r: set(v) for r, v in snap["ids"].items()}
        s.idems = {(x[0], x[1]): [x[2], x[3]] for x in snap["idems"]}
        s.chains = copy.deepcopy(snap["chains"])
        s.open_by, s.open_n = {}, {r: 0 for r in s.heads}
        for r, p in s.props.items():
            for i, d in p.items():
                if d["status"] == "open":
                    s.open_by.setdefault((r, d["author"]), set()).add(i)
                    s.open_n[r] += d["author_kind"] != "human"
        return s

    def copy(self):
        return MemoryState.restore(json.loads(canon(self.snapshot())))
