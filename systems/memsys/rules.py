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


def norm(v: str) -> str:
    return " ".join(v.casefold().split())


def resolve_slot(claims, rank=TRUST_RANK):
    """Return {claim_id: (status, valid_to)}."""
    out = {}
    cur: Optional[Claim] = None
    for c in sorted(claims, key=lambda c: c.order_key):
        if c.modality in NON_ASSERTIVE:
            out[c.id] = (NON_ASSERTIVE[c.modality], None)
            continue
        if cur is None:
            out[c.id] = ("current", None)
            cur = c
            continue
        same = norm(c.object) == norm(cur.object)
        r_new, r_cur = rank[c.trust_origin], rank[cur.trust_origin]
        if same and r_new <= r_cur:
            out[c.id] = ("duplicate", None)
        elif r_new >= r_cur:
            # equal or higher trust closes the current claim (same value at
            # higher trust is a re-assertion that raises the slot's trust)
            out[cur.id] = ("superseded", c.valid_from)
            out[c.id] = ("current", None)
            cur = c
        else:
            out[c.id] = ("disputed", None)
    return out
