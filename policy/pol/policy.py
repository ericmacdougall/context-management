"""Client-supplied policy: format, strict validator, and the Policy value every decision reads. No concrete policy ships: an empty or
broken document is the deny-everything policy; EXAMPLE_POLICY is marked as an example.

Every Policy (from a document OR built in code) is validated and canonicalised in __post_init__, so in-code construction cannot skip
what the parser enforces.

Model matching rule (M3). A destination pins a model id that must be DECLARED in `models` (directly or through an alias):
  models = {model_id: {"family": f, "version": v}}
  - model_id, family and version are canonical model ids (ids.canon_model). model_id must equal the family or start with the family
    followed by one of - _ . : @ / (so a suffixed or tagged variant is declared under its family by construction).
  - A ban spec is "f" (bans every declared model of family f: all versions, suffixes and tags) or "f@v" (bans family f at version v,
    all tags). Family membership comes from the declaration, never from string prefixes: a ban on "gpt" does NOT cover family
    "gpt-oss" (over-match is not used as a safety net); an undeclared pinned model denies (`destination_model_undeclared`).
  - The same spec form is used in labels (`banned` and the `models` allow list); an allow-list entry "f", "f@v" or a declared model id.
  - Aliases are canonicalised, followed to a fixed point, must end at a declared model id, and a cycle is a PolicyError.
  - (R3-6) A declared model is an immutable snapshot: its version is concrete (a digit, no floating word such as latest, stable,
    current, preview, *) and its id carries that version after the family ("gpt-x@2025-01", "m-local@1"). An unversioned or floating
    id is refused at parse time, so the id checked before a call is the id that is called. The after-call served-model check in
    GuardedChat is a DETECTOR only: by then the request has reached the provider; a mismatch disables the destination."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, FrozenSet, Mapping, Optional, Tuple

from .ids import IdError, MODEL_SEPARATORS, canon, canon_model

ACTIONS = ("append", "read", "inject", "send_model", "embed", "send_inbox", "export", "erase")
DEST_KINDS = ("model", "embedder", "export")


class PolicyError(Exception):
    pass


FLOATING = frozenset(["latest", "stable", "current", "default", "preview", "beta", "alpha", "nightly", "canary", "main", "head", "any",
                      "new", "newest", "auto", "rolling", "edge", "next", "experimental", "*"])


def _tokens(x):
    t = [x]
    for sep in MODEL_SEPARATORS:
        t = [y for z in t for y in z.split(sep)]
    return t


def _pinned(m, f, v):
    """R3-6: a declared model is an IMMUTABLE snapshot. The version must be concrete (it contains a digit and no floating word), and the
    model id itself must carry that version after the family (the provider is called with the id, so an id without its version, such as
    "gpt-x" or "gpt-x:latest", may be served as another model). Floating tags anywhere in the id are refused."""
    if not v or not any(c.isdigit() for c in v) or set(_tokens(v)) & FLOATING:
        raise PolicyError("models: version %r of %r is not a concrete pinned version" % (v, m))
    rest = m[len(f) + 1:] if m != f else ""
    if set(_tokens(rest)) & FLOATING or "*" in m:
        raise PolicyError("models: id %r carries a floating tag" % (m,))
    if not any(rest == v or rest.startswith(v + sep) or rest.endswith(sep + v) or (sep + v + sep) in rest
               for sep in MODEL_SEPARATORS):
        raise PolicyError("models: id %r does not carry its version %r (unversioned ids may float at the provider)" % (m, v))


@dataclass(frozen=True)
class ModelDecl:
    family: str
    version: str


@dataclass(frozen=True)
class Dest:
    kind: str                           # model | embedder | export
    region: Optional[str]
    local: bool                         # declared, not attested (see DESIGN.md: local endpoint attestation is not proven)
    max_sensitivity: int
    model: Optional[str]                # declared model id or alias (model and embedder kinds); None for export
    programme: Optional[str]
    tenants: FrozenSet[str]             # contracted tenants: REQUIRED (no "any tenant" default)


@dataclass(frozen=True)
class Authorization:
    principal: str
    tenant: str
    programme: str
    expires: float


def _fs(x, name, fn=canon):
    if isinstance(x, (str, bytes)) or not isinstance(x, (set, frozenset, list, tuple)):
        raise PolicyError(name + ": must be a collection")
    try:
        return frozenset(fn(i) for i in x)
    except IdError as e:
        raise PolicyError(name + ": " + str(e))


@dataclass(frozen=True)
class Policy:
    version: int
    grants: Mapping[str, FrozenSet[str]] = field(default_factory=dict)          # tenant -> actions; EMPTY = deny everything
    destinations: Mapping[str, Dest] = field(default_factory=dict)
    models: Mapping[str, ModelDecl] = field(default_factory=dict)
    model_aliases: Mapping[str, str] = field(default_factory=dict)
    banned_models: FrozenSet[str] = frozenset()
    tenant_banned: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    deny_tenants: FrozenSet[str] = frozenset()
    revoked: FrozenSet[str] = frozenset()
    authorizations: Mapping[str, Authorization] = field(default_factory=dict)
    extra_rules: Tuple[Tuple[str, Callable], ...] = ()
    conflict_notice: str = "none"                                                 # none (default) | existence_only (opt-in)
    hold_events: FrozenSet[str] = frozenset()

    def __post_init__(self):
        s = lambda k, v: object.__setattr__(self, k, v)
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 0:
            raise PolicyError("version")
        try:
            grants = {}
            for t, acts in dict(self.grants).items():
                a = frozenset(acts) if not isinstance(acts, str) else None
                if a is None or not a <= set(ACTIONS):
                    raise PolicyError("grants: unknown action")
                grants[canon(t)] = a
            s("grants", MappingProxyType(grants))
            models = {}
            for mid, d in dict(self.models).items():
                if isinstance(d, Mapping):
                    if set(d) != {"family", "version"}:
                        raise PolicyError("models: entry keys must be family, version")
                    d = ModelDecl(d["family"], d["version"])
                if not isinstance(d, ModelDecl):
                    raise PolicyError("models: entry type")
                m, f, v = canon_model(mid), canon_model(d.family), canon_model(d.version)
                if not (m == f or (m.startswith(f) and m[len(f)] in MODEL_SEPARATORS)):
                    raise PolicyError("models: id %r is not family %r or a suffixed/tagged variant of it" % (m, f))
                _pinned(m, f, v)
                models[m] = ModelDecl(f, v)
            s("models", MappingProxyType(models))
            aliases = {canon_model(k): canon_model(v) for k, v in dict(self.model_aliases).items()}
            resolved = {}
            for a in aliases:
                seen, cur = {a}, aliases[a]
                while cur in aliases:
                    if cur in seen:
                        raise PolicyError("model_aliases: cycle through %r" % cur)
                    seen.add(cur)
                    cur = aliases[cur]
                if cur not in models:
                    raise PolicyError("model_aliases: %r resolves to undeclared model %r" % (a, cur))
                resolved[a] = cur
            s("model_aliases", MappingProxyType(resolved))
            s("banned_models", _fs(self.banned_models, "banned_models", canon_model))
            s("tenant_banned", MappingProxyType({canon(t): _fs(x, "tenant_banned", canon_model) for t, x in dict(self.tenant_banned).items()}))
            s("deny_tenants", _fs(self.deny_tenants, "deny_tenants"))
            s("revoked", _fs(self.revoked, "revoked"))
            s("hold_events", _fs(self.hold_events, "hold_events"))
            dests = {}
            for n, d in dict(self.destinations).items():
                if not isinstance(d, Dest) or d.kind not in DEST_KINDS or not isinstance(d.local, bool) or not isinstance(d.max_sensitivity, int) \
                        or isinstance(d.max_sensitivity, bool) or not 0 <= d.max_sensitivity <= 3 or not (d.region is None or isinstance(d.region, str)):
                    raise PolicyError("destination fields: %r" % n)
                model = None
                if d.kind in ("model", "embedder"):
                    if not isinstance(d.model, str):
                        raise PolicyError("destination model pin required: %r" % n)
                    model = canon_model(d.model)          # resolved and checked against the registry at decision time
                prog = None if d.programme is None else canon(d.programme)
                dests[n] = replace(d, model=model, programme=prog, tenants=_fs(d.tenants, "destination tenants"))
            s("destinations", MappingProxyType(dests))
            auths = {}
            for i, a in dict(self.authorizations).items():
                if isinstance(a, Mapping):
                    if set(a) != {"principal", "tenant", "programme", "expires"}:
                        raise PolicyError("authorizations keys")
                    a = Authorization(a["principal"], a["tenant"], a["programme"], a["expires"])
                if not isinstance(a, Authorization) or not isinstance(a.expires, (int, float)) or isinstance(a.expires, bool):
                    raise PolicyError("authorizations")
                auths[i] = Authorization(canon(a.principal), canon(a.tenant), canon(a.programme), float(a.expires))
            s("authorizations", MappingProxyType(auths))
        except IdError as e:
            raise PolicyError(str(e))
        if self.conflict_notice not in ("existence_only", "none"):
            raise PolicyError("conflict_notice")
        if not isinstance(self.extra_rules, tuple) or not all(isinstance(r, tuple) and len(r) == 2 and callable(r[1]) for r in self.extra_rules):
            raise PolicyError("extra_rules")

    def with_changes(self, changes: Mapping[str, Any]) -> "Policy":
        """A tightened (or changed) copy with version + 1."""
        return replace(self, version=self.version + 1, **dict(changes))

    # -------------------------------------------------------------- model resolution (pure)
    def resolve_model(self, model_id) -> Optional[str]:
        try:
            m = canon_model(model_id)
        except IdError:
            return None
        m = self.model_aliases.get(m, m)
        return m if m in self.models else None

    def spec_matches(self, spec, model_id) -> bool:
        """Does a ban or allow-list spec ("family", "family@version" or a declared model id) cover this declared model?"""
        m = self.resolve_model(model_id)
        if m is None:
            return False
        try:
            sp = canon_model(spec)
        except IdError:
            return False
        sp = self.model_aliases.get(sp, sp)
        d = self.models[m]
        return sp == m or sp == d.family or sp == d.family + "@" + d.version


DENY_ALL = Policy(0)


def parse_policy(doc) -> Policy:
    """Strict validator for the client policy document. Unknown keys, wrong types, unknown actions, undeclared or cyclic models raise
    PolicyError. Format: {"version", "grants", "destinations", "models", "model_aliases", "banned_models", "tenant_banned", "deny_tenants",
    "revoked", "hold_events", "authorizations", "conflict_notice", "example"?}."""
    if not isinstance(doc, dict):
        raise PolicyError("document must be an object")
    allowed = {"version", "grants", "destinations", "models", "banned_models", "tenant_banned", "deny_tenants", "revoked", "hold_events",
               "authorizations", "conflict_notice", "model_aliases", "example"}
    if set(doc) - allowed:
        raise PolicyError("unknown keys")
    v = doc.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise PolicyError("version")

    def lst(x, name):
        if not isinstance(x, list) or not all(isinstance(i, str) and i for i in x):
            raise PolicyError(name)
        return x

    def dct(x, name):
        if not isinstance(x, dict) or not all(isinstance(k, str) and k for k in x):
            raise PolicyError(name)
        return x
    grants = {t: lst(a, "grants actions") for t, a in dct(doc.get("grants", {}), "grants").items()}
    dests = {}
    for n, d in dct(doc.get("destinations", {}), "destinations").items():
        if not isinstance(d, dict) or set(d) - {"kind", "region", "local", "max_sensitivity", "model", "programme", "tenants"} or "tenants" not in d:
            raise PolicyError("destination keys")
        if d.get("kind") not in DEST_KINDS:
            raise PolicyError("destination kind")
        for k in ("model", "programme", "region"):
            if d.get(k) is not None and not isinstance(d.get(k), str):
                raise PolicyError("destination " + k)
        dests[n] = Dest(d["kind"], d.get("region"), d.get("local"), d.get("max_sensitivity"), d.get("model"), d.get("programme"),
                        lst(d["tenants"], "destination tenants"))
    models = {}
    for m, d in dct(doc.get("models", {}), "models").items():
        if not isinstance(d, dict) or set(d) != {"family", "version"} or not all(isinstance(d[k], str) and d[k] for k in d):
            raise PolicyError("models entry")
        models[m] = d
    aliases = dct(doc.get("model_aliases", {}), "model_aliases")
    if not all(isinstance(x, str) and x for x in aliases.values()):
        raise PolicyError("model_aliases")
    tb = {t: lst(x, "tenant_banned") for t, x in dct(doc.get("tenant_banned", {}), "tenant_banned").items()}
    auths = dct(doc.get("authorizations", {}), "authorizations")
    for a in auths.values():
        if not isinstance(a, dict) or set(a) != {"tenant", "programme", "expires", "principal"} or not all(isinstance(a[k], str) for k in ("tenant", "programme", "principal")):
            raise PolicyError("authorizations")
    cn = doc.get("conflict_notice", "none")
    if not isinstance(cn, str):
        raise PolicyError("conflict_notice")
    try:
        return Policy(v, grants=grants, destinations=dests, models=models, model_aliases=aliases,
                      banned_models=lst(doc.get("banned_models", []), "banned_models"), tenant_banned=tb,
                      deny_tenants=lst(doc.get("deny_tenants", []), "deny_tenants"), revoked=lst(doc.get("revoked", []), "revoked"),
                      hold_events=lst(doc.get("hold_events", []), "hold_events"), authorizations=auths, conflict_notice=cn)
    except PolicyError:
        raise
    except Exception as e:      # noqa: BLE001  any other construction error is a policy error
        raise PolicyError(type(e).__name__)


def safe_policy(doc) -> Policy:
    """Load a client policy; on ANY error return the deny-everything policy."""
    try:
        return parse_policy(doc)
    except Exception:       # noqa: BLE001
        return DENY_ALL


EXAMPLE_POLICY = {"example": True, "version": 1, "grants": {"tenant-a": ["append", "read", "inject", "send_model", "erase"]},
                  "models": {"example-model@1": {"family": "example-model", "version": "1"}},
                  "destinations": {"eu-local-llm": {"kind": "model", "region": "EU", "local": True, "max_sensitivity": 3, "model": "example-model@1",
                                                    "tenants": ["tenant-a"]}}}
