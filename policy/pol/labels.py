"""Label lattice. A Label is carried by every record; derived data gets join(inputs), the most restrictive of each dimension.

Dimensions and how each one gets MORE restrictive:
  tenants      set grows (a set of more than one tenant is unreadable by everyone: no cross-tenant derivation)
  acl          CNF: a set of clauses, each a set of groups; a principal needs >= 1 group from EVERY clause. Join = union of
               clauses (readers = intersection of the inputs' readers). Absorbed clauses (supersets) are dropped.
  regions      None = any region, else the set of regions the bytes may be processed or stored in; join = intersection
  sensitivity  0 public .. 3 restricted; join = max
  models       None = any model, else the allowed model ids; join = intersection
  banned       model ids that must never see it; join = union
  local_only   required local inference; join = OR
  restricted   restricted security channel only; join = OR
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional, Sequence

SENSITIVITY_MAX = 3
DIMENSIONS = ("tenants", "acl", "regions", "sensitivity", "models", "banned", "local_only", "restricted", "holds", "subjects")


class LabelError(Exception):
    pass


def _fs_str(name, v, allow_none=False):
    if v is None and allow_none:
        return None
    if not isinstance(v, (set, frozenset, tuple, list)):
        raise LabelError(name + ": not a collection")
    out = frozenset(v)
    if not all(isinstance(x, str) and x for x in out):
        raise LabelError(name + ": members must be non-empty str")
    return out


def _norm_acl(clauses):
    cl = set()
    for c in clauses:
        c = _fs_str("acl clause", c)
        if not c:
            raise LabelError("acl: empty clause (unsatisfiable) is not allowed; use a distinct unreachable group")
        cl.add(c)
    keep = {c for c in cl if not any(o < c for o in cl)}      # absorption: a superset clause is implied by its subset
    return frozenset(keep)


@dataclass(frozen=True)
class Label:
    tenants: FrozenSet[str]
    acl: FrozenSet[FrozenSet[str]] = frozenset()
    regions: Optional[FrozenSet[str]] = None
    sensitivity: int = 0
    models: Optional[FrozenSet[str]] = None
    banned: FrozenSet[str] = frozenset()
    local_only: bool = False
    restricted: bool = False
    holds: FrozenSet[str] = frozenset()      # legal-hold ids covering this data; join = union
    subjects: FrozenSet[str] = frozenset()   # data subjects this record mentions (also what others said about them); join = union

    def __post_init__(self):
        s = object.__setattr__
        t = _fs_str("tenants", self.tenants)
        if not t:
            raise LabelError("tenants: empty")
        s(self, "tenants", t)
        if not isinstance(self.acl, (set, frozenset, tuple, list)):
            raise LabelError("acl: not a collection")
        s(self, "acl", _norm_acl(self.acl))
        s(self, "regions", _fs_str("regions", self.regions, True))
        if not isinstance(self.sensitivity, int) or isinstance(self.sensitivity, bool) or not 0 <= self.sensitivity <= SENSITIVITY_MAX:
            raise LabelError("sensitivity: int 0..%d" % SENSITIVITY_MAX)
        s(self, "models", _fs_str("models", self.models, True))
        s(self, "banned", _fs_str("banned", self.banned))
        s(self, "holds", _fs_str("holds", self.holds))
        s(self, "subjects", _fs_str("subjects", self.subjects))
        if not isinstance(self.local_only, bool) or not isinstance(self.restricted, bool):
            raise LabelError("flags must be bool")

    def to_dict(self):
        return {"tenants": sorted(self.tenants), "acl": sorted(sorted(c) for c in self.acl), "regions": None if self.regions is None else sorted(self.regions),
                "sensitivity": self.sensitivity, "models": None if self.models is None else sorted(self.models), "banned": sorted(self.banned),
                "local_only": self.local_only, "restricted": self.restricted,
                "holds": sorted(self.holds), "subjects": sorted(self.subjects)}

    @staticmethod
    def from_dict(d):
        return Label(tenants=d["tenants"], acl=d["acl"], regions=d["regions"], sensitivity=d["sensitivity"], models=d["models"], banned=d["banned"],
                     local_only=d["local_only"], restricted=d["restricted"], holds=d.get("holds", []), subjects=d.get("subjects", []))

    def digest(self):
        import hashlib
        parts = [sorted(self.tenants), sorted(sorted(c) for c in self.acl), None if self.regions is None else sorted(self.regions),
                 self.sensitivity, None if self.models is None else sorted(self.models), sorted(self.banned), self.local_only, self.restricted, sorted(self.holds), sorted(self.subjects)]
        return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]


def _inter(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def join2(a: Label, b: Label) -> Label:
    return Label(tenants=a.tenants | b.tenants, acl=a.acl | b.acl, regions=_inter(a.regions, b.regions),
                 sensitivity=max(a.sensitivity, b.sensitivity), models=_inter(a.models, b.models), banned=a.banned | b.banned,
                 local_only=a.local_only or b.local_only, restricted=a.restricted or b.restricted,
                 holds=a.holds | b.holds, subjects=a.subjects | b.subjects)


def join(labels: Iterable[Label]) -> Label:
    ls = list(labels)
    if not ls:
        raise LabelError("join of nothing")
    for l in ls:
        if not isinstance(l, Label):
            raise LabelError("not a Label")
    out = ls[0]
    for l in ls[1:]:
        out = join2(out, l)
    return out


def dim_leq(dim: str, a: Label, b: Label) -> bool:
    """True when b is at least as restrictive as a in this dimension."""
    if dim == "tenants":
        return a.tenants <= b.tenants
    if dim == "acl":
        return all(any(cb <= ca for cb in b.acl) for ca in a.acl)
    if dim == "regions":
        return a.regions is None or (b.regions is not None and b.regions <= a.regions)
    if dim == "sensitivity":
        return a.sensitivity <= b.sensitivity
    if dim == "models":
        return a.models is None or (b.models is not None and b.models <= a.models)
    if dim == "banned":
        return a.banned <= b.banned
    if dim == "local_only":
        return (not a.local_only) or b.local_only
    if dim == "restricted":
        return (not a.restricted) or b.restricted
    if dim == "holds":
        return a.holds <= b.holds
    if dim == "subjects":
        return a.subjects <= b.subjects
    raise LabelError("unknown dimension " + dim)


def violated_dims(a: Label, b: Label) -> Sequence[str]:
    """Dimensions in which b is LESS restrictive than a (a is an input, b the derived label)."""
    return [d for d in DIMENSIONS if not dim_leq(d, a, b)]


def leq(a: Label, b: Label) -> bool:
    return not violated_dims(a, b)


def acl_satisfied(label: Label, groups: Iterable[str]) -> bool:
    g = set(groups)
    return all(c & g for c in label.acl)


def join_dicts(*ds):
    """Join of label dicts; any missing (None) label makes the result None, and None is read as unlabelled = denied."""
    if not ds or any(d is None for d in ds):
        return None
    return join(Label.from_dict(d) for d in ds).to_dict()
