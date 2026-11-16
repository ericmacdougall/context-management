"""Policy decision point: one pure function, decide(principal, action, resource_labels, context, policy=...) -> Decision.

Rules, in the order they are evaluated (first failing rule names the reason):
  default deny; any error or non-boolean rule result denies; identity attributes (tenant, id, groups, clearance, ...) come
  only from the Principal and can never be overwritten by credential or derived attributes (a clash denies); labels of all
  inputs are joined at decision time; tenant, access list, clearance, restricted flag, then action-specific destination rules
  (region, model allow list and bans, local inference, sensitivity ceiling, restricted programme authorisation); finally the
  policy's extra rules, each of which must return exactly True.
Destination attributes come from the Policy (trusted), never from the request context: a caller can name a destination but
cannot describe it.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple

from .labels import Label, LabelError, acl_satisfied, join

ACTIONS = ("append", "read", "inject", "send_model", "embed", "send_inbox", "export", "erase")
KINDS = ("model", "embedder", "export")
ACTION_KIND = {"send_model": "model", "embed": "embedder", "export": "export"}

IDENTITY_KEYS = frozenset(["tenant", "tenants", "id", "subject", "subject.id", "subject.identifier", "principal", "groups", "group",
                           "clearance", "restricted.ok", "kind", "region", "model", "tenant.id", "user", "user.id"])
CREDENTIAL_NARROWING_KEYS = frozenset(["cap_clearance", "cap_actions", "cap_restricted"])


@dataclass(frozen=True)
class Principal:
    id: str
    tenant: str
    groups: FrozenSet[str] = frozenset()
    clearance: int = 0
    restricted_ok: bool = False
    kind: str = "human"                 # human | agent | analyst (an analyst acts with the rights of the principal it serves)
    region: Optional[str] = None        # where this principal (session) is served; used for inbox delivery
    model: Optional[str] = None         # destination id the principal's prompts go to (None: a human reading a screen)

    def validate(self):
        if not (isinstance(self.id, str) and self.id and isinstance(self.tenant, str) and self.tenant):
            raise ValueError("principal id and tenant must be non-empty str")
        if not isinstance(self.groups, (set, frozenset)) or not all(isinstance(g, str) for g in self.groups):
            raise ValueError("groups")
        if not isinstance(self.clearance, int) or isinstance(self.clearance, bool) or not 0 <= self.clearance <= 3:
            raise ValueError("clearance")
        if not isinstance(self.restricted_ok, bool) or self.kind not in ("human", "agent", "analyst"):
            raise ValueError("flags")


@dataclass(frozen=True)
class Dest:
    kind: str                           # model | embedder | export
    region: Optional[str]
    local: bool
    max_sensitivity: int
    model: Optional[str] = None         # the pinned model id behind the endpoint (model and embedder kinds)
    programme: Optional[str] = None     # restricted-channel programme this endpoint belongs to
    tenants: Optional[FrozenSet[str]] = None   # None = any tenant; else the contracted tenants


@dataclass(frozen=True)
class Policy:
    version: int
    destinations: Mapping[str, Dest] = field(default_factory=dict)
    banned_models: FrozenSet[str] = frozenset()
    tenant_banned: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    deny_tenants: FrozenSet[str] = frozenset()
    revoked: FrozenSet[str] = frozenset()
    authorizations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # id -> {tenant, programme, expires}
    extra_rules: Tuple[Tuple[str, Callable], ...] = ()
    conflict_notice: str = "existence_only"      # existence_only | none (see threads.py)
    grants: Mapping[str, FrozenSet[str]] = field(default_factory=dict)   # tenant -> actions the tenant's policy allows; EMPTY = deny everything
    hold_events: FrozenSet[str] = frozenset()    # legal holds that have a RECORDED hold event; a label's hold only binds once recorded

    def tighten(self, **changes) -> "Policy":
        return replace(self, version=self.version + 1, **changes)


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    policy_version: Any
    action: str = ""


def _norm_key(k):
    s = unicodedata.normalize("NFKC", k)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = s.strip().casefold()
    for ch in ("_", "-", " ", "/", ":"):
        s = s.replace(ch, ".")
    return s


class _Deny(Exception):
    pass


def _deny(reason):
    raise _Deny(reason)


def effective_caps(principal: Principal, context: Mapping) -> Dict[str, Any]:
    """Merge credential and derived attributes under the identity-first rule. Identity attributes are NOT part of the merge:
    they are read from the Principal. A credential or derived key that normalises to an identity key is a clash (deny); a
    non-string key is a deny; only the narrowing keys are interpreted (they can only reduce rights)."""
    caps: Dict[str, Any] = {}
    for src in ("credential", "derived"):
        attrs = context.get(src)
        if attrs is None:
            continue
        if not isinstance(attrs, dict):
            _deny("attr_source_type")
        for k, v in attrs.items():
            if not isinstance(k, str):
                _deny("attr_key_type")
            nk = _norm_key(k)
            if nk in IDENTITY_KEYS:
                _deny("attr_clash")
            if k in CREDENTIAL_NARROWING_KEYS:
                if k in caps and caps[k] != v:
                    _deny("attr_clash")          # the same narrowing key from two sources that disagree
                caps[k] = v
    return caps


def _dest(policy: Policy, dest_id, want_kind, label: Label, tenant: str, ctx: Mapping):
    if not isinstance(dest_id, str) or dest_id not in policy.destinations:
        _deny("unknown_destination")
    d = policy.destinations[dest_id]
    if d.kind != want_kind:
        _deny("destination_kind")
    if d.tenants is not None and tenant not in d.tenants:
        _deny("destination_tenant_scope")
    if want_kind in ("model", "embedder"):
        m = d.model
        if m is None:
            _deny("destination_model_unpinned")
        if m in policy.banned_models or m in policy.tenant_banned.get(tenant, frozenset()) or m in label.banned:
            _deny("model_banned")
        if label.models is not None and m not in label.models:
            _deny("model_not_allowed")
    if label.local_only and not d.local:
        _deny("local_inference_required")
    if label.regions is not None and (d.region is None or d.region not in label.regions):
        _deny("region_forbidden")
    if d.max_sensitivity < label.sensitivity:
        _deny("sensitivity_above_destination")
    if label.restricted:
        if d.programme is None:
            _deny("restricted_channel_required")
        auth = policy.authorizations.get(ctx.get("authorization"))
        now = ctx.get("now")
        if (auth is None or auth.get("tenant") != tenant or auth.get("programme") != d.programme
                or not isinstance(now, (int, float)) or isinstance(now, bool) or not isinstance(auth.get("expires"), (int, float))
                or auth["expires"] <= now):
            _deny("restricted_authorization_missing")


def _region_known(label: Label, ctx: Mapping, region):
    if label.regions is not None and (not isinstance(region, str) or region not in label.regions):
        _deny("region_forbidden")


def _evaluate(principal, action, resource_labels, context, policy) -> str:
    if action not in ACTIONS:
        _deny("unknown_action")
    if not isinstance(policy, Policy):
        _deny("no_policy")
    if not isinstance(principal, Principal):
        _deny("principal_type")
    principal.validate()
    if not isinstance(context, dict):
        _deny("context_type")
    if isinstance(resource_labels, Label):
        resource_labels = [resource_labels]
    labels = list(resource_labels) if resource_labels is not None else []
    if not labels:
        _deny("unlabelled")
    label = join(labels)                      # most restrictive of all inputs, at the moment of use
    caps = effective_caps(principal, context)
    if "cap_actions" in caps:
        ca = caps["cap_actions"]
        if not isinstance(ca, (set, frozenset, list, tuple)) or action not in ca:
            _deny("credential_scope")
    if action not in policy.grants.get(principal.tenant, frozenset()):
        _deny("no_grant")                        # default deny: an empty policy grants nothing
    if principal.tenant in policy.deny_tenants:
        _deny("tenant_killed")
    if principal.id in policy.revoked:
        _deny("principal_revoked")
    if label.tenants != frozenset([principal.tenant]):
        _deny("tenant_mismatch")
    if not acl_satisfied(label, principal.groups):
        _deny("acl")
    clearance = principal.clearance
    if "cap_clearance" in caps:
        cc = caps["cap_clearance"]
        if not isinstance(cc, int) or isinstance(cc, bool):
            _deny("attr_value_type")
        clearance = min(clearance, cc)
    if clearance < label.sensitivity:
        _deny("clearance")
    r_ok = principal.restricted_ok and (caps.get("cap_restricted", True) is True)
    if "cap_restricted" in caps and not isinstance(caps["cap_restricted"], bool):
        _deny("attr_value_type")
    if label.restricted and not r_ok:
        _deny("restricted_clearance")

    t = principal.tenant
    if action == "erase":
        # precedence rule: a legal hold beats erasure only under a recorded hold event; the erasure is then queued, not refused for good
        if label.holds & policy.hold_events:
            _deny("legal_hold_erasure_queued")
        _region_known(label, context, context.get("region"))
    elif action in ("append", "read"):
        _region_known(label, context, context.get("region"))
    elif action == "inject":
        _region_known(label, context, context.get("region"))
        if context.get("destination") is not None:
            _dest(policy, context["destination"], "model", label, t, context)
    elif action in ("send_model", "embed", "export"):
        _dest(policy, context.get("destination"), ACTION_KIND[action], label, t, context)
    elif action == "send_inbox":
        rcp = context.get("recipient")
        if not isinstance(rcp, Principal):
            _deny("recipient_type")
        rcp.validate()
        if rcp.tenant != t or rcp.tenant in policy.deny_tenants:
            _deny("recipient_tenant")
        if rcp.id in policy.revoked:
            _deny("recipient_revoked")
        if not acl_satisfied(label, rcp.groups):
            _deny("recipient_acl")
        if rcp.clearance < label.sensitivity:
            _deny("recipient_clearance")
        if label.restricted and not rcp.restricted_ok:
            _deny("recipient_restricted_clearance")
        _region_known(label, context, rcp.region)
        if rcp.model is not None:
            _dest(policy, rcp.model, "model", label, t, context)
        elif label.local_only or label.models is not None:
            pass        # a human screen: no inference happens here
    for name, fn in policy.extra_rules:
        ok = fn(principal, action, label, context)
        if ok is not True:
            _deny("extra_rule_non_true:" + str(name))
    return "allowed"


def decide(principal, action, resource_labels, context, *, policy) -> Decision:
    ver = getattr(policy, "version", None)
    act = action if isinstance(action, str) else ""
    try:
        reason = _evaluate(principal, action, resource_labels, context, policy)
        return Decision(reason == "allowed", reason, ver, act)
    except _Deny as d:
        return Decision(False, str(d), ver, act)
    except LabelError as e:
        return Decision(False, "label_error", ver, act)
    except BaseException as e:          # any evaluation error denies; BaseException so a raising __bool__ or KeyboardInterrupt-like stub cannot allow
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return Decision(False, "eval_error:" + type(e).__name__, ver, act)


# ------------------------------------------------------------------------------------------------ policy format
class PolicyError(Exception):
    pass


DENY_ALL = Policy(0)


def parse_policy(doc) -> Policy:
    """Strict validator for the client-supplied policy document (JSON-able dict). Unknown keys, wrong types, unknown actions or
    dangling references raise PolicyError. Format:
      {"version": int, "grants": {tenant: [action,...]}, "destinations": {id: {kind, region, local, max_sensitivity, model?, programme?, tenants?}},
       "banned_models": [...], "tenant_banned": {tenant: [...]}, "deny_tenants": [...], "revoked": [...], "hold_events": [...],
       "authorizations": {id: {tenant, programme, expires}}, "conflict_notice": "existence_only"|"none"}"""
    if not isinstance(doc, dict):
        raise PolicyError("document must be an object")
    allowed = {"version", "grants", "destinations", "banned_models", "tenant_banned", "deny_tenants", "revoked", "hold_events", "authorizations", "conflict_notice", "example"}
    if set(doc) - allowed:
        raise PolicyError("unknown keys")
    v = doc.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise PolicyError("version")
    def strs(x, name):
        if not isinstance(x, list) or not all(isinstance(i, str) and i for i in x):
            raise PolicyError(name)
        return frozenset(x)
    grants = {}
    if not isinstance(doc.get("grants", {}), dict):
        raise PolicyError("grants")
    for t, acts in doc.get("grants", {}).items():
        if not isinstance(t, str) or not t:
            raise PolicyError("grants tenant")
        a = strs(acts, "grants actions")
        if a - set(ACTIONS):
            raise PolicyError("unknown action")
        grants[t] = a
    dests = {}
    if not isinstance(doc.get("destinations", {}), dict):
        raise PolicyError("destinations")
    for n, d in doc.get("destinations", {}).items():
        if not isinstance(d, dict) or set(d) - {"kind", "region", "local", "max_sensitivity", "model", "programme", "tenants"}:
            raise PolicyError("destination keys")
        if d.get("kind") not in KINDS or not isinstance(d.get("local"), bool) or not isinstance(d.get("max_sensitivity"), int) or isinstance(d.get("max_sensitivity"), bool) \
                or not 0 <= d["max_sensitivity"] <= 3 or not (d.get("region") is None or isinstance(d.get("region"), str)):
            raise PolicyError("destination fields")
        if d["kind"] in ("model", "embedder") and not isinstance(d.get("model"), str):
            raise PolicyError("destination model pin required")
        dests[n] = Dest(d["kind"], d.get("region"), d["local"], d["max_sensitivity"], d.get("model"), d.get("programme"),
                        None if d.get("tenants") is None else strs(d["tenants"], "dest tenants"))
    tb = doc.get("tenant_banned", {})
    if not isinstance(tb, dict):
        raise PolicyError("tenant_banned")
    auths = doc.get("authorizations", {})
    if not isinstance(auths, dict) or not all(isinstance(a, dict) and set(a) == {"tenant", "programme", "expires"} for a in auths.values()):
        raise PolicyError("authorizations")
    cn = doc.get("conflict_notice", "existence_only")
    if cn not in ("existence_only", "none"):
        raise PolicyError("conflict_notice")
    return Policy(v, destinations=dests, banned_models=strs(doc.get("banned_models", []), "banned_models"),
                  tenant_banned={t: strs(x, "tenant_banned") for t, x in tb.items()}, deny_tenants=strs(doc.get("deny_tenants", []), "deny_tenants"),
                  revoked=strs(doc.get("revoked", []), "revoked"), authorizations=dict(auths), conflict_notice=cn, grants=grants,
                  hold_events=strs(doc.get("hold_events", []), "hold_events"))


def safe_policy(doc) -> Policy:
    """Load a client policy; on ANY error return the deny-everything policy."""
    try:
        return parse_policy(doc)
    except Exception:       # noqa: BLE001
        return DENY_ALL


EXAMPLE_POLICY = {"example": True, "version": 1, "grants": {"tenant-a": ["append", "read", "inject", "send_model", "erase"]},
                  "destinations": {"eu-local-llm": {"kind": "model", "region": "EU", "local": True, "max_sensitivity": 3, "model": "example-model-1"}}}
