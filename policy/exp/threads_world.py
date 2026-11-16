"""Thread worlds for E5 (experiment code; the production choke points are pol.enforce Store, Outbox and Store.context).

Items live in threads with different access groups; typed edges (conflicts_with, depends_on, references) cross threads; analyst
findings join items of different threads. A viewer's view is assembled ONLY through production calls made with the viewer's own
issued session: Store.read for every record and for every cross-reference expansion (one decision per reference), Store.context
for prompt injection, an Outbox(send_inbox) from the analyst for findings. The pre-rebuild world (pol/threads.py at 77ea895) also
built conflict notices for store items; the rebuilt production has no store-item notice (the decided default is NONE), so this world
emits none, and conflict notices are measured on the production notice path (DecisionService) in E5's viewer-scoped arm."""
import json
import random
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from pol.directory import Directory, Principal, SessionAuthority
from pol.labels import Label
from pol.policy import parse_policy


@dataclass
class Item:
    id: str
    thread: str
    text: str
    label: Label
    tokens: List[str]


@dataclass
class World:
    items: Dict[str, Item]
    edges: List[Tuple[str, str, str]]
    findings: List[Tuple[str, Tuple[str, ...]]]
    groups: Dict[str, str]                    # thread -> group
    order: List[str] = field(default_factory=list)


def hexs(rng, n=8):
    return "%0*x" % (n, rng.getrandbits(4 * n))


def gen_world(rng):
    nthreads = rng.choice([2, 3])
    threads = ["thr%d" % i for i in range(nthreads)]
    group_of = {t: "grp-" + hexs(rng, 6) for t in threads}
    items, by_thread, order = {}, {t: [] for t in threads}, []

    def mk(thread, tenant, groups):
        iid = "ITEM-" + hexs(rng)
        toks = [iid, "MK-" + hexs(rng), "THREADNAME-" + hexs(rng), "VAL-" + hexs(rng), "DEC-" + hexs(rng)]
        text = "req %s title %s | value=%s | decider=%s | %s" % (toks[1], toks[2], toks[3], toks[4], iid)
        lab = Label([tenant], acl=[groups] if groups else [])
        items[iid] = Item(iid, thread, text, lab, toks)
        order.append(iid)
        return iid
    for t in threads:
        for _ in range(rng.randint(2, 4)):
            groups = [group_of[t]]
            if rng.random() < 0.2:
                groups.append(group_of[rng.choice([x for x in threads if x != t])])        # shared item: either group may read
            by_thread[t].append(mk(t, "t1", groups))
    for _ in range(rng.randint(1, 2)):
        mk("t2thr", "t2", None)                                                               # other-tenant noise
    ids = [i for i in order if items[i].label.tenants == frozenset(["t1"])]
    edges = []
    for _ in range(rng.randint(3, 7)):
        a, b = rng.sample(ids, 2)
        if items[a].thread == items[b].thread and rng.random() < 0.7:
            continue
        edges.append((a, b, rng.choice(["conflicts_with", "conflicts_with", "depends_on", "references"])))
    if not any(items[a].thread != items[b].thread and t == "conflicts_with" for a, b, t in edges) and rng.random() < 0.95:
        edges.append((rng.choice(by_thread[threads[0]]), rng.choice(by_thread[threads[1]]), "conflicts_with"))
    t2ids = [i for i in order if items[i].label.tenants == frozenset(["t2"])]
    if t2ids and rng.random() < 0.4:
        edges.append((rng.choice(ids), rng.choice(t2ids), "conflicts_with"))
    findings = []
    for _ in range(rng.randint(3, 5)):
        a, b = rng.sample(ids, 2)
        if items[a].thread == items[b].thread:
            continue
        findings.append(("FIND-" + hexs(rng), (a, b)))
    return World(items, edges, findings, group_of, order)


def viewers(world):
    gs = sorted(world.groups.values())
    vs = [("p%d" % k, "t1", [g]) for k, g in enumerate(gs)]
    vs += [("pall", "t1", gs), ("pnone", "t1", []), ("pt", "t2", [])]
    return vs


def all_groups(world):
    return sorted({g for it in world.items.values() for c in it.label.acl for g in c})


def build_view(E, world, viewer):
    """Everything `viewer` (id, tenant, groups) observes, through production calls with the viewer's own session."""
    vid, vten, vgroups = viewer
    ps = [Principal(v[0], v[1], frozenset(v[2]), 3, False, "human", "eu", None) for v in viewers(world)]
    ps.append(Principal("an", "t1", frozenset(all_groups(world)), 3, False, "analyst", "eu", None))
    d = Directory(ps)
    sa = SessionAuthority(d)
    pol = parse_policy({"version": 1, "grants": {"t1": ["append", "read", "inject", "send_inbox", "send_model"], "t2": ["read", "inject"]},
                        "models": {"m-eu-1": {"family": "m-eu", "version": "1"}},
                        "destinations": {"d_local": {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-eu-1",
                                                     "tenants": ["t1", "t2"]}}})
    audit = E.AuditLog()
    enf = E.Enforcer(lambda: pol, d, sa, audit)
    store = E.Store(enf)
    rid = {}
    for iid in world.order:
        it = world.items[iid]
        rid[iid] = store._put(sorted(it.label.tenants)[0], it.text, it.label, ())          # planted
    an = sa.issue("an", "agent_api")
    for fid, ins in world.findings:
        text = "finding %s (" % fid + " + ".join(world.items[i].text for i in ins) + ")"
        rid[fid] = store.append_derived(an, text, [rid[i] for i in ins])
    vs = sa.issue(vid, "owner_ui")
    view = {"records": []}
    for k in [rid[i] for i in world.order] + [rid[f] for f, _ in world.findings]:
        try:
            view["records"].append({"id": k, "text": store.read(vs, k).text})
        except E.PolicyDenied:
            pass
    ok, withheld = store.context(vs, [rid[i] for i in world.order], "d_local")
    view["injection"] = [store._get(k).text for k in ok]
    delivered = []
    ob = E.Outbox(enf, store, "send_inbox", lambda s, r, texts, q: delivered.extend(texts))
    for fid, _ins in world.findings:
        try:
            ob.enqueue(an, [rid[fid]], {"recipient_id": vid})
        except E.PolicyDenied:
            pass
    ob.deliver_all()
    view["inbox"] = delivered
    visible = {r["id"] for r in view["records"]}
    xrefs = []
    for iid in world.order:
        if rid[iid] not in visible:
            continue
        for (s, t, typ) in world.edges:
            other = t if s == iid else s if t == iid else None
            if other is None:
                continue
            try:
                xrefs.append({"from": rid[iid], "edge": typ, "id": rid[other], "text": store.read(vs, rid[other]).text})
            except E.PolicyDenied:
                pass
    view["xrefs"] = xrefs
    return view


def readable_by(label, viewer):
    _vid, vten, vgroups = viewer
    return label.tenants == frozenset([vten]) and all(set(c) & set(vgroups) for c in label.acl)


def mutate(world, viewer):
    """The same world with everything the viewer cannot read replaced: item ids and tokens, finding ids, and group names."""
    vgroups = set(viewer[2])
    salt = [0]
    hidden = {i for i, it in world.items.items() if not readable_by(it.label, viewer)}
    mp = {}
    for iid in world.order:
        if iid in hidden:
            salt[0] += 1
            mp[iid] = "ITEM-mut%04d" % salt[0]
    ren = lambda g: g if g in vgroups else g + "-mut"          # noqa: E731
    items, order = {}, []
    for iid in world.order:
        it = world.items[iid]
        if iid in hidden:
            n = mp[iid]
            toks = [n, "MK-mut%04d" % salt[0], "THREADNAME-mut", "VAL-mut", "DEC-mut"]
            text = "req %s title %s | value=%s | decider=%s | %s" % (toks[1], toks[2], toks[3], toks[4], n)
            lab = Label(it.label.tenants, acl=[[ren(g) for g in c] for c in it.label.acl])
            items[n] = Item(n, it.thread, text, lab, toks)
            order.append(n)
        else:
            items[iid] = it
            order.append(iid)
    m = lambda i: mp.get(i, i)                                 # noqa: E731
    edges = [(m(a), m(b), t) for a, b, t in world.edges]
    findings = []
    for k, (fid, ins) in enumerate(world.findings):
        vis = all(i not in hidden for i in ins)
        findings.append((fid if vis else "FIND-mut%d" % k, tuple(m(i) for i in ins)))
    return World(items, edges, findings, world.groups, order)


def serialise(view):
    return json.dumps(view, sort_keys=True)


def sources(world):
    """Raw ground truth for the verifier: each token with the labels it derives from (an item: its own; a finding id: its inputs)."""
    out = [{"tokens": it.tokens, "labels": [it.label.to_dict()]} for it in world.items.values()]
    out += [{"tokens": [fid], "labels": [world.items[i].label.to_dict() for i in ins]} for fid, ins in world.findings]
    return out
