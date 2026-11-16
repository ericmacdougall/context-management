"""Independent reference implementation of the decision core (GoodQA #16096).

Written from policy/CORE-RULES.md alone. Standard library only; imports nothing
from `pol` or `exp`.

API:
    core = RefCore(config)
    code, original_code = core.apply(event)
    core.view()   # comparable view of spec section 8
    core.audit    # list of {"code": code, "event": event} for every refused event

Design: apply() first EVALUATES the event against the current state without
mutating anything (returns a code and, for applied events, a commit closure),
then runs the closure and the bookkeeping only if the code is not a refusal.

AMBIGUITIES (each with the choice made)
 1. "valid values" of a label dict is not defined. Chosen: tenants, banned, holds,
    subjects are lists of str; acl is a list of lists of str; regions and models are
    None or a list of str; sensitivity is an int (not bool) >= 0; local_only and
    restricted are bool. Elements may be any str (including empty); duplicates allowed.
 2. Required-field types: every field named in the "Types" paragraph of section 2 is
    type-checked whenever it is PRESENT, even if the event type does not require it
    (e.g. a stray non-str `choose` on a propose is bad_event). Extra unknown fields are
    allowed, but the event must be canonical-JSON serialisable or it is bad_event.
 3. An invalid `role` on grant (not "decide"/"propose") is a shape violation (bad_event,
    rule 1), not a later check.
 4. For decide with an unknown verb only `verb` (plus the common fields, req, based_on)
    is required at shape level; the unknown verb itself is bad_event at rule 10e (after
    the authority / channel / agent_not_permitted checks).
 5. For decide resolve, if `choose` is present it wins; value_ref/label are then not
    required. If `choose` is absent, both value_ref and label must be present or the
    event is bad_event (shape).
 6. Default requirement label: tenants [tenant], acl [], regions None, sensitivity 0,
    models None, banned [], local_only False, restricted False, holds [], subjects [].
    A configured req label missing keys is filled from this default.
 7. The proposal id is the id of the propose event; the pending id is the id of the
    remark event. Author kind is recorded at creation as the config kind of the author.
 8. A grant event has a req, so its id IS recorded as used on that req when applied, but
    duplicate_id is never checked for grant (rule 6 applies before rule 8). Revoke has no
    req, so its id is never recorded on a req.
 9. Contesting (rule 14) changes only status (contested) and value_ref (None) plus
    version/last/contested; value_event, value_by, value_version, value_label are left as
    they were. Resolve restores all six value fields from contested.prior first.
10. (CLARIFIED by spec) Retract (rule 15 and the resolve retract effect) sets status retracted,
    value_ref None, value_label None, value_event = the event (the resolve for a resolve effect),
    value_by = principal, value_version = version + 1, contested None, pushing the current value
    to the chain only if value_ref is not None. The resolve retract effect has no
    nothing_to_retract check.
11. Resolve does not change proposal/pending statuses for a set effect (only a reject
    effect marks its proposal rejected, with closed_by = the resolve event). Joining or
    contesting with an accept/amend/confirm does not change the adopted content's status.
12. Resolve by `choose` uses the chosen candidate's stored computed label for a set effect;
    resolve by value uses join(requirement label, event label). last.label of a resolve is
    that label (None for reject/retract effects).
13. The same principal may join a contested requirement as a candidate more than once, and
    a candidate may repeat another candidate's effect (no rule forbids it).
14. In the contested-join path, the check "principal is non-human or any existing candidate's
    author is non-human" uses the config kind of the candidate's `by` principal.
15. Non-human decide with verb reject is handled by rule 11 before any contested check,
    even when the requirement is contested. Non-human accept while contested is `stale`
    (rule 12) when based_on equals the contested base; otherwise per rule 12's last bullet.
16. Idempotency of a refused event: refused events never record keys, so a retry after a
    refusal is evaluated afresh. `duplicate`/`idem_conflict` are audited too (the spec lists
    duplicate as refused; idem_conflict is in the refused list).
17. Principals revoked check (`principal_revoked`) applies after the `unauthorized`
    known-principal check, as ordered in rule 4. Revocation of an already revoked or
    self principal is allowed (an admin could revoke itself; afterwards it is revoked).
18. Candidate/last label for reject and retract is None; join of labels always normalises
    (sorted, deduplicated), so even a single label is written canonically.
19. The open caps count proposals and pending changes with status `open` authored by the
    principal on that requirement (per-principal) or all such on the requirement (per-req).
20. An applied but unchanged event (`concurrent_agree`) still appends to the event log and
    records its idem key and id.
"""

import copy
import hashlib
import json

MAX_CANDIDATES = 8
MAX_OPEN_PER_PRINCIPAL = 16
MAX_OPEN_PER_REQ = 256
OWNER_CHANNELS = ("owner_ui", "owner_cli")
AGENT_CHANNEL = "agent_api"

LABEL_KEYS = ("tenants", "acl", "regions", "sensitivity", "models", "banned",
              "local_only", "restricted", "holds", "subjects")

COMMON_STR = ("id", "idem", "tenant", "principal", "channel")
OPTIONAL_STR = ("req", "value_ref", "proposal", "proposal_hash", "pending",
                "choose", "target", "verb", "role")

REFUSED = {
    "bad_event", "tenant_mismatch", "unauthorized", "principal_revoked", "unknown_req",
    "duplicate_id", "proposal_cap", "channel_mismatch", "agent_not_permitted",
    "agent_cannot_confirm", "unknown_pending", "pending_closed", "same_channel_confirmation",
    "unknown_proposal", "self_approval", "proposal_mismatch", "stale", "proposal_closed",
    "candidates_full", "contested_needs_resolve", "not_contested", "nothing_to_retract",
    "unknown_candidate", "idem_conflict", "duplicate", "no_value_change",
}

HEAD_TYPES = ("confirm", "decide", "supersede", "retract")

# required fields per type besides the five common string fields
REQUIRED = {
    "propose": ("req", "value_ref", "label", "based_on"),
    "remark": ("req", "value_ref", "label", "based_on"),
    "confirm": ("req", "pending", "based_on"),
    "decide": ("req", "verb", "based_on"),
    "supersede": ("req", "value_ref", "label", "based_on"),
    "retract": ("req", "based_on"),
    "revoke": ("target",),
    "grant": ("req", "target", "role"),
}


# ---------------------------------------------------------------- helpers

def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def sha(o):
    return hashlib.sha256(canon(o).encode("utf-8")).hexdigest()


def _is_str(x):
    return isinstance(x, str) and x != ""


def _str_list(x):
    return isinstance(x, list) and all(isinstance(s, str) for s in x)


def valid_label(lab):
    if not isinstance(lab, dict):
        return False
    if set(lab.keys()) != set(LABEL_KEYS):
        return False
    if not _str_list(lab["tenants"]):
        return False
    acl = lab["acl"]
    if not isinstance(acl, list) or not all(_str_list(c) for c in acl):
        return False
    for k in ("regions", "models"):
        if lab[k] is not None and not _str_list(lab[k]):
            return False
    s = lab["sensitivity"]
    if isinstance(s, bool) or not isinstance(s, int) or s < 0:
        return False
    for k in ("banned", "holds", "subjects"):
        if not _str_list(lab[k]):
            return False
    for k in ("local_only", "restricted"):
        if not isinstance(lab[k], bool):
            return False
    return True


def join_labels(labels):
    """Join (and canonicalise) one or more valid label dicts."""
    tenants, banned, holds, subjects = set(), set(), set(), set()
    clauses = set()
    regions = None
    models = None
    sens = 0
    local_only = False
    restricted = False
    for lab in labels:
        tenants |= set(lab["tenants"])
        banned |= set(lab["banned"])
        holds |= set(lab["holds"])
        subjects |= set(lab["subjects"])
        for c in lab["acl"]:
            clauses.add(frozenset(c))
        if lab["regions"] is not None:
            regions = set(lab["regions"]) if regions is None else (regions & set(lab["regions"]))
        if lab["models"] is not None:
            models = set(lab["models"]) if models is None else (models & set(lab["models"]))
        sens = max(sens, lab["sensitivity"])
        local_only = local_only or lab["local_only"]
        restricted = restricted or lab["restricted"]
    kept = [c for c in clauses if not any(d < c for d in clauses)]
    return {
        "tenants": sorted(tenants),
        "acl": sorted(sorted(c) for c in kept),
        "regions": None if regions is None else sorted(regions),
        "sensitivity": sens,
        "models": None if models is None else sorted(models),
        "banned": sorted(banned),
        "local_only": local_only,
        "restricted": restricted,
        "holds": sorted(holds),
        "subjects": sorted(subjects),
    }


def default_label(tenant):
    return {"tenants": [tenant], "acl": [], "regions": None, "sensitivity": 0,
            "models": None, "banned": [], "local_only": False, "restricted": False,
            "holds": [], "subjects": []}


def shape_ok(ev):
    """Rule 1: True when the event has a valid shape."""
    if not isinstance(ev, dict):
        return False
    t = ev.get("type")
    if not isinstance(t, str) or t not in REQUIRED:
        return False
    for k in COMMON_STR:
        if k not in ev or not _is_str(ev[k]):
            return False
    for k in REQUIRED[t]:
        if k not in ev:
            return False
    for k in OPTIONAL_STR:
        if k in ev and not _is_str(ev[k]):
            return False
    if "based_on" in ev:
        b = ev["based_on"]
        if isinstance(b, bool) or not isinstance(b, int) or b < 0:
            return False
    if "label" in ev and not valid_label(ev["label"]):
        return False
    if t == "grant" and ev["role"] not in ("decide", "propose"):
        return False
    if t == "decide":
        v = ev["verb"]
        if v in ("accept", "reject"):
            if "proposal" not in ev or "proposal_hash" not in ev:
                return False
        elif v == "amend":
            for k in ("proposal", "proposal_hash", "value_ref", "label"):
                if k not in ev:
                    return False
        elif v == "resolve":
            if "choose" not in ev:
                if "value_ref" not in ev or "label" not in ev:
                    return False
    try:
        canon(ev)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- core

class RefCore:
    def __init__(self, config):
        self.tenant = config["tenant"]
        self.principals = dict(config["principals"])
        self.authority = {r: list(v) for r, v in config["authority"].items()}
        props = config.get("proposers") or {}
        self.proposers = {r: list(props.get(r, [])) for r in self.authority}
        self.admins = set(config.get("admins") or [])
        given = config.get("req_labels") or {}
        self.req_label = {}
        for r in self.authority:
            lab = default_label(self.tenant)
            if given.get(r):
                lab.update(given[r])
            self.req_label[r] = join_labels([lab])
        self.revoked = set()
        self.heads = {r: self._new_head() for r in self.authority}
        self.chain = {r: [] for r in self.authority}
        self.proposals = {r: {} for r in self.authority}
        self.pending = {r: {} for r in self.authority}
        self.used_ids = {r: set() for r in self.authority}
        self.idem = {}
        self.log = []
        self.audit = []

    @staticmethod
    def _new_head():
        return {"version": 0, "status": "unset", "value_ref": None, "value_label": None,
                "value_event": None, "value_by": None, "value_version": 0,
                "last": None, "contested": None}

    # ---- public

    def apply(self, ev):
        try:
            return self._apply(ev)
        except Exception:
            self._refuse("bad_event", ev)
            return ("bad_event", None)

    def _refuse(self, code, ev):
        try:
            self.audit.append({"code": code, "event": copy.deepcopy(ev)})
        except Exception:
            self.audit.append({"code": code, "event": ev})

    def _apply(self, ev):
        code, orig, commit = self._evaluate(ev)
        if code in REFUSED:
            self._refuse(code, ev)
            return (code, orig)
        if commit is not None:
            commit()
        # bookkeeping of an applied event
        self.idem[(ev["principal"], ev["idem"])] = (sha(ev), code)
        if "req" in ev:
            self.used_ids[ev["req"]].add(ev["id"])
        self.log.append(copy.deepcopy(ev))
        return (code, None)

    def view(self):
        reqs = {}
        for r, h in self.heads.items():
            c = h["contested"]
            reqs[r] = {
                "version": h["version"], "status": h["status"], "value_ref": h["value_ref"],
                "value_event": h["value_event"], "value_by": h["value_by"],
                "value_version": h["value_version"],
                "value_label": copy.deepcopy(h["value_label"]),
                "contested": None if c is None else {
                    "base": c["base"],
                    "candidates": [[x["event"], x["by"], copy.deepcopy(x["effect"]), x["verb"]]
                                   for x in c["candidates"]],
                },
            }
        return {
            "revoked": sorted(self.revoked),
            "authority": {r: sorted(set(v)) for r, v in self.authority.items()},
            "proposers": {r: sorted(set(v)) for r, v in self.proposers.items()},
            "reqs": reqs,
            "proposals": {r: {i: p["status"] for i, p in d.items()}
                          for r, d in self.proposals.items()},
            "pending": {r: {i: p["status"] for i, p in d.items()}
                        for r, d in self.pending.items()},
        }

    # ---- evaluation (no mutation); returns (code, original_code, commit_or_None)

    def _evaluate(self, ev):
        if not shape_ok(ev):                                          # rule 1
            return ("bad_event", None, None)
        if ev["tenant"] != self.tenant:                               # rule 2
            return ("tenant_mismatch", None, None)
        key = (ev["principal"], ev["idem"])                           # rule 3
        if key in self.idem:
            fp, orig = self.idem[key]
            if fp == sha(ev):
                return ("duplicate", orig, None)
            return ("idem_conflict", orig, None)
        p = ev["principal"]
        if p not in self.principals:                                  # rule 4
            return ("unauthorized", None, None)
        if p in self.revoked:
            return ("principal_revoked", None, None)
        t = ev["type"]
        if t == "revoke":
            return self._revoke(ev)
        if t == "grant":
            return self._grant(ev)
        req = ev["req"]
        if req not in self.authority:                                 # rule 7
            return ("unknown_req", None, None)
        if ev["id"] in self.used_ids[req]:                            # rule 8
            return ("duplicate_id", None, None)
        if t in ("propose", "remark"):
            return self._propose(ev)
        return self._head_moving(ev)

    def _revoke(self, ev):
        p = ev["principal"]
        if p not in self.admins:
            return ("unauthorized", None, None)
        target = ev["target"]
        if target not in self.principals:
            return ("bad_event", None, None)

        def commit():
            self.revoked.add(target)
            for r in self.authority:
                for rec in list(self.proposals[r].values()) + list(self.pending[r].values()):
                    if rec["author"] == target and rec["status"] == "open":
                        rec["status"] = "invalidated"
        return ("revoked", None, commit)

    def _grant(self, ev):
        p = ev["principal"]
        if p not in self.admins:
            return ("unauthorized", None, None)
        req, target = ev["req"], ev["target"]
        if req not in self.authority:
            return ("bad_event", None, None)
        if target not in self.principals or target in self.revoked:
            return ("bad_event", None, None)

        def commit():
            lst = self.authority[req] if ev["role"] == "decide" else self.proposers[req]
            if target not in lst:
                lst.append(target)
        return ("granted", None, commit)

    def _open_counts(self, req, principal):
        mine = total = 0
        for rec in list(self.proposals[req].values()) + list(self.pending[req].values()):
            if rec["status"] == "open":
                total += 1
                if rec["author"] == principal:
                    mine += 1
        return mine, total

    def _propose(self, ev):
        p, req, t = ev["principal"], ev["req"], ev["type"]
        if p not in self.authority[req] and p not in self.proposers[req]:
            return ("unauthorized", None, None)
        mine, total = self._open_counts(req, p)
        if mine >= MAX_OPEN_PER_PRINCIPAL or total >= MAX_OPEN_PER_REQ:
            return ("proposal_cap", None, None)

        def commit():
            rec = {"status": "open", "author": p, "kind": self.principals[p],
                   "value_ref": ev["value_ref"], "label": join_labels([ev["label"]]),
                   "base": ev["based_on"], "closed_by": None}
            if t == "propose":
                self.proposals[req][ev["id"]] = rec
            else:
                rec["channel"] = ev["channel"]
                self.pending[req][ev["id"]] = rec
        return ("proposed" if t == "propose" else "pending_created", None, commit)

    # ---- effect of a head-moving event

    def _effect(self, ev, prop, pend):
        """Return (effect, computed label or None, adopted record or None)."""
        t = ev["type"]
        R = self.req_label[ev["req"]]
        if t == "confirm":
            return (["set", pend["value_ref"]], join_labels([R, pend["label"]]), pend)
        if t == "supersede":
            return (["set", ev["value_ref"]], join_labels([R, ev["label"]]), None)
        if t == "retract":
            return (["retract"], None, None)
        verb = ev["verb"]
        if verb == "accept":
            return (["set", prop["value_ref"]], join_labels([R, prop["label"]]), prop)
        if verb == "amend":
            return (["set", ev["value_ref"]],
                    join_labels([R, prop["label"], ev["label"]]), prop)
        # reject
        return (["reject", ev["proposal"]], None, prop)

    @staticmethod
    def _adopted_ok(adopted, closers):
        if adopted is None or adopted["status"] == "open":
            return True
        return adopted["closed_by"] is not None and adopted["closed_by"] in closers

    @staticmethod
    def _snap(h):
        return {"value_ref": h["value_ref"], "status": h["status"],
                "value_event": h["value_event"], "value_by": h["value_by"],
                "value_version": h["value_version"],
                "value_label": copy.deepcopy(h["value_label"])}

    def _set_value(self, req, ev, ref, label, new_version):
        h = self.heads[req]
        if h["value_ref"] is not None:
            self.chain[req].append(self._snap(h))
        h["value_ref"] = ref
        h["status"] = "current"
        h["value_event"] = ev["id"]
        h["value_by"] = ev["principal"]
        h["value_label"] = copy.deepcopy(label)
        h["value_version"] = new_version
        h["contested"] = None

    def _retract_value(self, req, ev, new_version):
        h = self.heads[req]
        if h["value_ref"] is not None:
            self.chain[req].append(self._snap(h))
        h["status"] = "retracted"
        h["value_ref"] = None
        h["value_label"] = None
        h["value_event"] = ev["id"]
        h["value_by"] = ev["principal"]
        h["value_version"] = new_version
        h["contested"] = None

    def _head_moving(self, ev):
        t, p, req, ch, bo = ev["type"], ev["principal"], ev["req"], ev["channel"], ev["based_on"]
        h = self.heads[req]
        human = self.principals[p] == "human"
        verb = ev["verb"] if t == "decide" else None
        if p not in self.authority[req]:                                      # 10a
            return ("unauthorized", None, None)
        if human and ch not in OWNER_CHANNELS:                                # 10b
            return ("channel_mismatch", None, None)
        if not human and ch != AGENT_CHANNEL:
            return ("channel_mismatch", None, None)
        if not human and (t in ("supersede", "retract")
                          or (t == "decide" and verb in ("amend", "resolve"))):   # 10c
            return ("agent_not_permitted", None, None)
        prop = pend = None
        if t == "confirm":                                                    # 10d
            if not human:
                return ("agent_cannot_confirm", None, None)
            pend = self.pending[req].get(ev["pending"])
            if pend is None:
                return ("unknown_pending", None, None)
            if pend["status"] != "open":
                return ("pending_closed", None, None)
            if pend["channel"] == ch:
                return ("same_channel_confirmation", None, None)
        if t == "decide":                                                     # 10e
            if verb not in ("accept", "reject", "amend", "resolve"):
                return ("bad_event", None, None)
            if verb != "resolve":
                prop = self.proposals[req].get(ev["proposal"])
                if prop is None:
                    return ("unknown_proposal", None, None)
                if verb == "accept" and not human and prop["kind"] != "human":
                    return ("self_approval", None, None)
                if ev["proposal_hash"] != sha([ev["proposal"], prop["value_ref"], prop["author"]]):
                    return ("proposal_mismatch", None, None)

        # rule 11: non-human reject
        if t == "decide" and verb == "reject" and not human:
            if bo != h["version"]:
                return ("stale", None, None)
            if prop["status"] != "open":
                return ("proposal_closed", None, None)

            def commit_nh():
                prop["status"] = "rejected"
                prop["closed_by"] = ev["id"]
            return ("decided_reject", None, commit_nh)

        # rule 12: contested requirement
        c = h["contested"]
        if c is not None:
            if t == "decide" and verb == "resolve":
                if bo != h["version"]:
                    return ("stale", None, None)
                return self._resolve(ev, c)
            if bo == c["base"]:
                effect, label, adopted = self._effect(ev, prop, pend)
                if not human or any(self.principals.get(x["by"]) != "human"
                                    for x in c["candidates"]):
                    return ("stale", None, None)
                if len(c["candidates"]) >= MAX_CANDIDATES:
                    return ("candidates_full", None, None)
                if not self._adopted_ok(adopted, {x["event"] for x in c["candidates"]}):
                    return ("proposal_closed", None, None)

                def commit_join():
                    c["candidates"].append({"event": ev["id"], "by": p, "effect": effect,
                                            "verb": verb if t == "decide" else t,
                                            "label": label})
                return ("contested_joined", None, commit_join)
            if bo == h["version"]:
                return ("contested_needs_resolve", None, None)
            return ("stale", None, None)

        # rule 13
        if t == "decide" and verb == "resolve":
            if bo != h["version"]:
                return ("stale", None, None)
            return ("not_contested", None, None)

        # rule 14: stale binding
        if bo != h["version"]:
            last = h["last"]
            sibling = (last is not None and bo == last["based_on"]
                       and h["version"] == bo + 1 and last["by"] != p
                       and last["verb"] != "resolve" and human
                       and self.principals.get(last["by"]) == "human")
            if not sibling:
                return ("stale", None, None)
            effect, label, adopted = self._effect(ev, prop, pend)
            if not self._adopted_ok(adopted, {last["event"]}):
                return ("proposal_closed", None, None)
            if effect == last["effect"]:
                return ("concurrent_agree", None, None)

            def commit_contest():
                hh = self.heads[req]
                lst = hh["last"]
                hh["contested"] = {
                    "base": bo, "prior": copy.deepcopy(lst["prev"]),
                    "candidates": [
                        {"event": lst["event"], "by": lst["by"], "effect": lst["effect"],
                         "verb": lst["verb"], "label": lst["label"]},
                        {"event": ev["id"], "by": p, "effect": effect,
                         "verb": verb if t == "decide" else t, "label": label},
                    ]}
                hh["status"] = "contested"
                hh["value_ref"] = None
                hh["version"] += 1
                hh["last"] = None
            return ("contested_opened", None, commit_contest)

        # rule 15: normal application
        effect, label, adopted = self._effect(ev, prop, pend)
        if t == "decide" and verb in ("accept", "reject", "amend") and prop["status"] != "open":
            return ("proposal_closed", None, None)
        # Amendment 3 (spec change applied by the rescue owner, not the reference author): a non-human accept of the current value
        if (t == "decide" and verb == "accept" and not human and h["status"] == "current"
                and effect[1] == h["value_ref"]):
            return ("no_value_change", None, None)
        if t == "retract" and h["status"] in ("unset", "retracted"):
            return ("nothing_to_retract", None, None)
        if t == "decide":
            code = {"accept": "decided_accept", "reject": "decided_reject",
                    "amend": "decided_amend"}[verb]
        else:
            code = {"supersede": "superseded", "retract": "retracted",
                    "confirm": "confirmed_applied"}[t]

        def commit_normal():
            hh = self.heads[req]
            prev = self._snap(hh)
            newv = hh["version"] + 1
            if t == "decide" and verb in ("accept", "amend"):
                prop["status"] = "accepted"
                prop["closed_by"] = ev["id"]
            elif t == "decide":      # human reject
                prop["status"] = "rejected"
                prop["closed_by"] = ev["id"]
            elif t == "confirm":
                pend["status"] = "confirmed"
                pend["closed_by"] = ev["id"]
            if effect[0] == "set":
                self._set_value(req, ev, effect[1], label, newv)
            elif effect[0] == "retract":
                self._retract_value(req, ev, newv)
            hh["version"] = newv
            hh["last"] = {"event": ev["id"], "by": p, "based_on": bo, "effect": effect,
                          "verb": verb if t == "decide" else t, "prev": prev, "label": label}
        return (code, None, commit_normal)

    # ---- resolve (section 4)

    def _resolve(self, ev, c):
        req, p = ev["req"], ev["principal"]
        R = self.req_label[req]
        if "choose" in ev:
            cand = None
            for x in c["candidates"]:
                if x["event"] == ev["choose"]:
                    cand = x
                    break
            if cand is None:
                return ("unknown_candidate", None, None)
            effect, label = copy.deepcopy(cand["effect"]), cand["label"]
        else:
            effect, label = ["set", ev["value_ref"]], join_labels([R, ev["label"]])

        def commit():
            h = self.heads[req]
            prior = c["prior"]
            for k in ("value_ref", "status", "value_event", "value_by", "value_version"):
                h[k] = prior[k]
            h["value_label"] = copy.deepcopy(prior["value_label"])
            newv = h["version"] + 1
            if effect[0] == "set":
                self._set_value(req, ev, effect[1], label, newv)
            elif effect[0] == "retract":
                self._retract_value(req, ev, newv)
            elif effect[0] == "reject":
                rec = self.proposals[req].get(effect[1])
                if rec is not None:
                    rec["status"] = "rejected"
                    rec["closed_by"] = ev["id"]
            h["contested"] = None
            h["version"] = newv
            h["last"] = {"event": ev["id"], "by": p, "based_on": ev["based_on"],
                         "effect": effect, "verb": "resolve",
                         "prev": copy.deepcopy(prior), "label": label}
        return ("resolved", None, commit)


# ---------------------------------------------------------------- self-check

def _selfcheck():
    T = "t1"

    def mk():
        return RefCore({
            "tenant": T,
            "principals": {"h1": "human", "h2": "human", "a1": "agent",
                           "an1": "analyst", "adm": "human"},
            "authority": {"R": ["h1", "h2", "a1"]},
            "proposers": {"R": ["an1"]},
            "admins": ["adm"],
        })

    def lab():
        return default_label(T)

    def ev(typ, principal, cid, channel="owner_ui", **kw):
        e = {"type": typ, "id": principal + "/" + cid, "idem": cid, "tenant": T,
             "principal": principal, "channel": channel}
        e.update(kw)
        return e

    def propose(core, principal, cid, ref, channel="owner_ui", based_on=0):
        return core.apply(ev("propose", principal, cid, channel, req="R", value_ref=ref,
                             label=lab(), based_on=based_on))

    def accept(principal, cid, pid, ref, author, based_on, channel="owner_ui"):
        return ev("decide", principal, cid, channel, req="R", verb="accept", proposal=pid,
                  proposal_hash=sha([pid, ref, author]), based_on=based_on)

    results = []

    def check(name, ok):
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + name)

    # 1 accept
    c = mk()
    r1 = propose(c, "h1", "p1", "v1")
    r2 = c.apply(accept("h2", "d1", "h1/p1", "v1", "h1", 0))
    v = c.view()["reqs"]["R"]
    check("accept", r1 == ("proposed", None) and r2 == ("decided_accept", None)
          and v["version"] == 1 and v["value_ref"] == "v1" and v["status"] == "current"
          and v["value_version"] == 1 and c.view()["proposals"]["R"] == {"h1/p1": "accepted"})

    # 2 contested sibling between two humans, then resolve
    c = mk()
    a = c.apply(ev("supersede", "h1", "s1", req="R", value_ref="X", label=lab(), based_on=0))
    b = c.apply(ev("supersede", "h2", "s2", req="R", value_ref="Y", label=lab(), based_on=0))
    v = c.view()["reqs"]["R"]
    ok = (a == ("superseded", None) and b == ("contested_opened", None)
          and v["status"] == "contested" and v["value_ref"] is None and v["version"] == 2
          and v["contested"] == {"base": 0, "candidates": [
              ["h1/s1", "h1", ["set", "X"], "supersede"],
              ["h2/s2", "h2", ["set", "Y"], "supersede"]]})
    stale_needs = c.apply(ev("supersede", "h1", "s3", req="R", value_ref="Z", label=lab(),
                             based_on=2))
    ok = ok and stale_needs == ("contested_needs_resolve", None)
    check("contested sibling", ok)
    rr = c.apply(ev("decide", "h1", "r1", req="R", verb="resolve", choose="h2/s2", based_on=2))
    v = c.view()["reqs"]["R"]
    check("resolve", rr == ("resolved", None) and v["status"] == "current"
          and v["value_ref"] == "Y" and v["version"] == 3 and v["contested"] is None
          and v["value_event"] == "h1/r1" and v["value_by"] == "h1")

    # 3 non-human reject leaves the version unchanged
    c = mk()
    propose(c, "h1", "p1", "v1")
    before = c.view()["reqs"]["R"]
    rj = c.apply(ev("decide", "a1", "d1", "agent_api", req="R", verb="reject", proposal="h1/p1",
                    proposal_hash=sha(["h1/p1", "v1", "h1"]), based_on=0))
    after = c.view()
    ok = (rj == ("decided_reject", None) and after["reqs"]["R"] == before
          and after["proposals"]["R"] == {"h1/p1": "rejected"})
    stale = c.apply(ev("decide", "a1", "d2", "agent_api", req="R", verb="reject",
                       proposal="h1/p1", proposal_hash=sha(["h1/p1", "v1", "h1"]), based_on=5))
    check("non-human reject", ok and stale == ("stale", None))

    # 4 proposal cap at 17 open proposals
    c = mk()
    codes = [propose(c, "an1", "q%d" % i, "v%d" % i)[0] for i in range(17)]
    check("proposal cap", codes[:16] == ["proposed"] * 16 and codes[16] == "proposal_cap"
          and len(c.audit) == 1 and c.audit[0]["code"] == "proposal_cap")

    # 5 idempotent duplicate returns the original code
    c = mk()
    e = ev("propose", "h1", "p1", req="R", value_ref="v1", label=lab(), based_on=0)
    f1 = c.apply(e)
    f2 = c.apply(copy.deepcopy(e))
    e2 = dict(copy.deepcopy(e), value_ref="v2")
    f3 = c.apply(e2)
    check("idempotent duplicate", f1 == ("proposed", None) and f2 == ("duplicate", "proposed")
          and f3 == ("idem_conflict", "proposed") and len(c.log) == 1)

    # 6 malformed events never raise
    c = mk()
    bad = [None, 5, [], {}, {"type": "propose"}, {"type": "nope"},
           ev("propose", "h1", "x", req="R", value_ref="v", label=lab(), based_on=True)]
    check("malformed", all(c.apply(b)[0] == "bad_event" for b in bad))

    print("ALL PASS" if all(results) else "SOME FAILED")
    return all(results)


if __name__ == "__main__":
    raise SystemExit(0 if _selfcheck() else 1)
