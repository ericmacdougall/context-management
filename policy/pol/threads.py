"""Parallel related threads: items live in threads with different access groups; typed edges (conflicts_with, depends_on,
references) cross threads. A viewer sees only what decide() allows. The one deliberate declassification is the conflict notice:
when a visible item L conflicts with an item the viewer cannot read, the viewer gets exactly

    {"kind": "conflict", "item": <L.id>, "version": <L.version>, "state": "contested_by_restricted_item"}

and nothing else: not the other item's id, thread, group, content, value, decider, version or timing, and not how many such
items exist. It has L's own label (it describes L, not X). Policy.conflict_notice == "none" switches even that off.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .enforce import Block, Enforcer, Outbox, PolicyDenied, Record, Store, derive, inject
from .labels import Label
from .pdp import Policy, Principal, decide

NOTICE_FIELDS = ("kind", "item", "version", "state")


def conflict_notice(item_id: str, version: int) -> dict:
    return {"kind": "conflict", "item": item_id, "version": version, "state": "contested_by_restricted_item"}


@dataclass
class Item:
    id: str
    thread: str
    text: str
    value: str
    decider: str
    version: int
    label: Label


@dataclass
class World:
    items: Dict[str, Item]
    edges: List[Tuple[str, str, str]]                 # (src, dst, type)
    findings: List[Tuple[str, Tuple[str, ...]]]       # (finding id, input item ids): analyst findings and shared decisions
    group_of_thread: Dict[str, str]


def build_view(world: World, viewer: Principal, policy: Policy, mode: Optional[str] = None) -> dict:
    """Everything `viewer` can observe from this world, assembled only through enforced choke points. mode selects a broken
    control: 'derive_first', 'notice_full', 'xref_no_check'."""
    analyst = Principal("analyst-for-" + viewer.id, viewer.tenant, viewer.groups, viewer.clearance, viewer.restricted_ok, kind="analyst", region=viewer.region)
    directory = {analyst.id: analyst, viewer.id: viewer}
    enf = Enforcer(lambda: policy, directory=directory.get, clock=lambda: 10)
    store = Store(enf, check_lineage=(mode != "derive_first"))
    dmode = "first" if mode == "derive_first" else "join"
    ctx = {}
    for it in world.items.values():
        store.records[it.id] = Record(it.id, it.text + " | value=" + it.value + " | decider=" + it.decider, it.label, (), "item", it.thread)
    for fid, ins in world.findings:
        blocks = [Block(store.records[i].text, store.records[i].label, (i,)) for i in ins]
        d = derive(blocks, "finding(" + " + ".join(b.text for b in blocks) + ")", dmode)
        store.records[fid] = Record(fid, d.text, d.label, d.lineage, "finding", "")
    ids = sorted(store.records)
    visible = store.retrieve(viewer, ids, ctx)
    vis_ids = {r.id for r in visible}
    view = {"records": [{"id": r.id, "text": r.text} for r in visible]}
    # prompt injection of everything visible
    blocks, withheld = inject(enf, viewer, [store.records[i] for i in ids], ctx)
    view["injection"] = [b.text for b in blocks]
    # analyst findings through the inbox (the analyst acts with the viewer's rights; the recipient must be entitled to every input)
    delivered = []
    ob = Outbox(enf, directory.get, "send_inbox", lambda p, b, c: delivered.append(b[0].text))
    for fid, _ins in world.findings:
        r = store.records[fid]
        try:
            ob.enqueue(analyst.id, [Block(r.text, r.label, r.lineage)], {}, viewer.id)
        except PolicyDenied:
            pass
    ob.deliver_all()
    view["inbox"] = delivered
    # cross references and conflict notices for each visible item
    xrefs, notices = [], []
    for it_id in sorted(vis_ids):
        it = world.items.get(it_id)
        if it is None:
            continue
        hidden_conflict = False
        for (s, d, typ) in world.edges:
            other = d if s == it_id else s if d == it_id else None
            if other is None:
                continue
            o = world.items[other]
            dd = decide(viewer, "read", o.label, ctx, policy=policy, trusted={"now": 10})
            readable = dd.allow
            if readable or mode == "xref_no_check":
                xrefs.append({"from": it_id, "edge": typ, "id": o.id, "text": o.text})
            elif typ == "conflicts_with" and dd.reason != "tenant_mismatch":      # never a signal across tenants
                hidden_conflict = True
        if hidden_conflict and policy.conflict_notice == "existence_only":
            n = conflict_notice(it.id, it.version)
            if mode == "notice_full":
                other_ids = [(d if s == it_id else s) for (s, d, t) in world.edges if t == "conflicts_with" and it_id in (s, d)]
                n = dict(n, other=world.items[other_ids[0]].id, other_text=world.items[other_ids[0]].text)
            notices.append(n)
    view["xrefs"] = xrefs
    view["notices"] = notices
    return view


def serialise(view: dict) -> bytes:
    return json.dumps(view, sort_keys=True).encode()
