"""Trust-gated supersession as a pure function of one slot's claims.

A slot is (subject, predicate, scope). resolve_slot() takes every claim of the
slot in any order, sorts by the stored canonical order key and returns the
status of each claim. Because the result depends only on the claim SET, live
incremental derivation, a rebuild from the log, and the removal of an erased
claim (which reinstates its predecessor) all agree.

Statuses: current, superseded, disputed, duplicate, hypothetical, proposed.
"""
from dataclasses import dataclass
from typing import Optional

# Higher outranks lower. A claim can close a current claim of rank <= its own.
TRUST_RANK = {"user_typed": 4, "tool": 3, "user_supplied_external": 2, "assistant": 2, "web": 1}
NON_ASSERTIVE = {"hypothetical": "hypothetical", "proposed": "proposed"}


@dataclass
class Claim:
    id: str
    object: str
    modality: str          # asserted | decided | proposed | hypothetical
    trust_origin: str
    valid_from: object     # comparable timestamp (datetime or ISO string)
    order_key: str         # canonical total order, stored with the claim
    scope: str = "*"       # '*' = whole slot; anything else narrows it (e.g. 'work')


def norm(v: str) -> str:
    return " ".join(v.casefold().split())


def resolve_slot(claims, rank=TRUST_RANK, cross_scope=True):
    """Resolve every claim of one (subject, predicate) group. Returns {claim_id: (status, valid_to)}.

    Within one scope a claim closes the current claim of rank <= its own. Across scopes: a scoped claim of LOWER
    trust than the current whole-slot ('*') claim, with a different value, is disputed (a narrower scope must not be a
    way around trust gating). cross_scope=False is the broken variant used by tests."""
    out = {}
    cur = {}                      # scope -> current Claim
    for c in sorted(claims, key=lambda c: c.order_key):
        if c.modality in NON_ASSERTIVE:
            out[c.id] = (NON_ASSERTIVE[c.modality], None)
            continue
        whole = cur.get("*")
        if (cross_scope and c.scope != "*" and whole is not None and rank[c.trust_origin] < rank[whole.trust_origin]
                and norm(c.object) != norm(whole.object)):
            out[c.id] = ("disputed", None)
            continue
        existing = cur.get(c.scope)
        if existing is None:
            out[c.id] = ("current", None)
            cur[c.scope] = c
            continue
        same = norm(c.object) == norm(existing.object)
        r_new, r_cur = rank[c.trust_origin], rank[existing.trust_origin]
        if same and r_new <= r_cur:
            out[c.id] = ("duplicate", None)
        elif r_new >= r_cur:
            out[existing.id] = ("superseded", c.valid_from)
            out[c.id] = ("current", None)
            cur[c.scope] = c
        else:
            out[c.id] = ("disputed", None)
    return out
