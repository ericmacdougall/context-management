"""Policy decision point (PRIVATE). evaluate(...) is a pure function of trusted inputs only: the principal and recipient come from the
Directory, `now` from the runtime clock, the policy from the policy source; all three are supplied by pol.enforce.Enforcer, never by
a request. The request may only NAME things: a destination id, an authorisation id, a recipient id, and narrowing credential claims.

Rules, in order (the first failing rule names the reason): default deny; any error or non-boolean rule result denies; labels of all inputs
are joined at decision time; credential claims may only narrow and only the exact keys cap_clearance, cap_actions, cap_restricted are
accepted (ANY other key, any non-str or non-ASCII key, denies: `attr_unrecognised_key`); tenant grant, kill switch, revocation (partial
skeleton over-match), tenant, access list, clearance, restricted flag, then the action's destination rules; finally the policy's extra
rules, each of which must return exactly True."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .directory import Principal
from .ids import IdError, skeleton
from .labels import Label, LabelError, _acl_satisfied, join
from .policy import ACTIONS, Policy

ACTION_KIND = {"send_model": "model", "embed": "embedder", "export": "export"}
CREDENTIAL_KEYS = frozenset(["cap_clearance", "cap_actions", "cap_restricted"])
REQUEST_KEYS = frozenset(["destination", "authorization", "recipient_id", "credential"])


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    policy_version: Any
    action: str


class _Deny(Exception):
    pass


def _deny(reason):
    raise _Deny(reason)


def _caps(credential):
    if credential is None:
        return {}
    if not isinstance(credential, dict):
        _deny("attr_source_type")
    for k in credential:
        if not isinstance(k, str) or not k.isascii() or k not in CREDENTIAL_KEYS:
            _deny("attr_unrecognised_key")
    return dict(credential)


def _revoked(policy, pid):
    try:
        sk = skeleton(pid)
    except IdError:
        return True
    return any(skeleton(r) == sk for r in policy.revoked)


def _model_ok(policy, model, label, tenant):
    m = policy.resolve_model(model)
    if m is None:
        _deny("destination_model_undeclared")
    bans = set(policy.banned_models) | set(policy.tenant_banned.get(tenant, frozenset())) | set(label.banned)
    if any(policy.spec_matches(b, m) for b in bans):
        _deny("model_banned")
    if label.models is not None and not any(policy.spec_matches(a, m) for a in label.models):
        _deny("model_not_allowed")


def _dest(policy, dest_id, want_kind, label, tenant, request, now, pid):
    if not isinstance(dest_id, str) or dest_id not in policy.destinations:
        _deny("unknown_destination")
    d = policy.destinations[dest_id]
    if d.kind != want_kind:
        _deny("destination_kind")
    if tenant not in d.tenants:
        _deny("destination_tenant_scope")
    if want_kind in ("model", "embedder"):
        _model_ok(policy, d.model, label, tenant)
    if label.local_only and not d.local:
        _deny("local_inference_required")
    if label.regions is not None and (d.region is None or d.region not in label.regions):
        _deny("region_forbidden")
    if d.max_sensitivity < label.sensitivity:
        _deny("sensitivity_above_destination")
    if label.restricted:
        if d.programme is None:
            _deny("restricted_channel_required")
        auth = policy.authorizations.get(request.get("authorization"))
        if (auth is None or auth.principal != pid or auth.tenant != tenant or auth.programme != d.programme
                or not isinstance(now, float) or auth.expires <= now):
            _deny("restricted_authorization_missing")


def _region(label, region):
    if label.regions is not None and (not isinstance(region, str) or region not in label.regions):
        _deny("region_forbidden")


def _evaluate(principal, action, labels, request, policy, now, recipient):
    if action not in ACTIONS:
        _deny("unknown_action")
    if not isinstance(policy, Policy):
        _deny("no_policy")
    if not isinstance(principal, Principal):
        _deny("principal_type")
    if not isinstance(request, dict) or set(request) - REQUEST_KEYS:
        _deny("forged_request")
    labels = list(labels)
    if not labels or not all(isinstance(l, Label) for l in labels):
        _deny("unlabelled")
    label = join(labels)
    caps = _caps(request.get("credential"))
    if "cap_actions" in caps:
        ca = caps["cap_actions"]
        if not isinstance(ca, (list, tuple, set, frozenset)) or action not in ca:
            _deny("credential_scope")
    t = principal.tenant
    if action not in policy.grants.get(t, frozenset()):
        _deny("no_grant")
    if t in policy.deny_tenants:
        _deny("tenant_killed")
    if _revoked(policy, principal.id):
        _deny("principal_revoked")
    if label.tenants != frozenset([t]):
        _deny("tenant_mismatch")
    if not _acl_satisfied(label, principal.groups):
        _deny("acl")
    clearance = principal.clearance
    if "cap_clearance" in caps:
        cc = caps["cap_clearance"]
        if not isinstance(cc, int) or isinstance(cc, bool):
            _deny("attr_value_type")
        clearance = min(clearance, cc)
    if clearance < label.sensitivity:
        _deny("clearance")
    if "cap_restricted" in caps and not isinstance(caps["cap_restricted"], bool):
        _deny("attr_value_type")
    r_ok = principal.restricted_ok and caps.get("cap_restricted", True) is True
    if label.restricted and not r_ok:
        _deny("restricted_clearance")
    if action == "erase":
        if label.holds & policy.hold_events:
            _deny("erasure_queued")             # a recorded legal hold wins; the erasure is queued (reason does not name the hold)
        _region(label, principal.region)
    elif action in ("append", "read"):
        _region(label, principal.region)
    elif action == "inject":
        _region(label, principal.region)
        _dest(policy, request.get("destination"), "model", label, t, request, now, principal.id)
    elif action in ACTION_KIND:
        _dest(policy, request.get("destination"), ACTION_KIND[action], label, t, request, now, principal.id)
    elif action == "send_inbox":
        rcp = recipient
        if not isinstance(rcp, Principal):
            _deny("recipient_unknown")
        if rcp.tenant != t or rcp.tenant in policy.deny_tenants:
            _deny("recipient_tenant")
        if _revoked(policy, rcp.id):
            _deny("recipient_revoked")
        if not _acl_satisfied(label, rcp.groups):
            _deny("recipient_acl")
        if rcp.clearance < label.sensitivity:
            _deny("recipient_clearance")
        if label.restricted and not rcp.restricted_ok:
            _deny("recipient_restricted_clearance")
        _region(label, rcp.region)
        if rcp.model is not None:
            _dest(policy, rcp.model, "model", label, t, request, now, rcp.id)
    for name, fn in policy.extra_rules:
        if fn(principal, action, label, request) is not True:
            _deny("extra_rule_non_true:" + str(name))
    return "allowed"


def evaluate(principal, action, labels, request, policy, now, recipient) -> Decision:
    ver = getattr(policy, "version", None)
    act = action if isinstance(action, str) else ""
    try:
        reason = _evaluate(principal, action, labels, request, policy, now, recipient)
        return Decision(reason == "allowed", reason, ver, act)
    except _Deny as d:
        return Decision(False, str(d), ver, act)
    except LabelError:
        return Decision(False, "label_error", ver, act)
    except BaseException as e:          # noqa: BLE001  any evaluation error denies
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return Decision(False, "eval_error:" + type(e).__name__, ver, act)
