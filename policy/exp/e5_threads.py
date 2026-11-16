"""E5: parallel related threads with different access groups. A member of one thread never receives content from the other
through records, injection, analyst findings, inbox, cross-reference expansion or conflict notices, yet still learns that a
conflict exists, and learns exactly what the notice contract allows. Non-interference: mutating everything the viewer cannot
read leaves the viewer's whole view byte-identical. Controls: derivation join dropped, notice carrying the other side,
cross-reference expansion without a decision.
usage: python -m exp.e5_threads"""
import json
import random

from exp import common  # noqa: F401
from exp.common import write_result
from pol.labels import Label
from exp.gen import GRANTS
from pol.pdp import Policy, Principal
from pol.threads import Item, World, build_view, conflict_notice, serialise

SEEDS = [11, 12, 13, 14, 15]
PER_SEED = 200


def hexs(rng, n=8):
    return "%0*x" % (n, rng.getrandbits(4 * n))


def gen_world(rng):
    nthreads = rng.choice([2, 3])
    threads = ["thr%d" % i for i in range(nthreads)]
    group_of = {t: "GRP-" + hexs(rng, 6) for t in threads}
    items = {}
    by_thread = {t: [] for t in threads}
    for t in threads:
        for _ in range(rng.randint(2, 4)):
            iid = "ITEM-" + hexs(rng)
            groups = [group_of[t]]
            if rng.random() < 0.2 and nthreads > 1:
                groups.append(group_of[rng.choice([x for x in threads if x != t])])      # shared item: either group may read
            lab = Label(["t1"], acl=[groups], regions=None, sensitivity=0)
            text = "req MK-%s title THREADNAME-%s" % (hexs(rng), hexs(rng))
            items[iid] = Item(iid, t, text, "VAL-" + hexs(rng), "DEC-" + hexs(rng), rng.randint(1, 4), lab)
            by_thread[t].append(iid)
    for _ in range(rng.randint(1, 2)):                                                    # other-tenant noise
        iid = "ITEM-" + hexs(rng)
        items[iid] = Item(iid, "t2thr", "req MK-%s other tenant THREADNAME-%s" % (hexs(rng), hexs(rng)), "VAL-" + hexs(rng), "DEC-" + hexs(rng), 1,
                          Label(["t2"]))
    edges = []
    ids = [i for i in items if items[i].label.tenants == frozenset(["t1"])]
    for _ in range(rng.randint(3, 7)):
        a, b = rng.sample(ids, 2)
        if items[a].thread == items[b].thread and rng.random() < 0.7:
            continue
        edges.append((a, b, rng.choice(["conflicts_with", "conflicts_with", "depends_on", "references"])))
    if not any(items[a].thread != items[b].thread and t == "conflicts_with" for a, b, t in edges) and rng.random() < 0.95:
        a = rng.choice(by_thread[threads[0]])
        b = rng.choice(by_thread[threads[1]])
        edges.append((a, b, "conflicts_with"))
    findings = []
    for _ in range(rng.randint(3, 5)):
        a, b = rng.sample(ids, 2)
        if items[a].thread == items[b].thread:
            continue
        findings.append(("FIND-" + hexs(rng), (a, b)))
    return World(items, edges, findings, group_of)


def principals(world):
    gs = list(world.group_of_thread.values())
    ps = [Principal("P_%s" % g, "t1", frozenset([g]), 3, True, region="EU") for g in gs]
    ps.append(Principal("P_all", "t1", frozenset(gs), 3, True, region="EU"))
    ps.append(Principal("P_none", "t1", frozenset(), 3, True, region="EU"))
    ps.append(Principal("P_t2", "t2", frozenset(), 3, True, region="EU"))
    return ps


def readable(it_label, p):
    return it_label.tenants == frozenset([p.tenant]) and all(set(c) & p.groups for c in it_label.acl)


def mutate(world, p):
    """Same world with everything the viewer cannot read replaced: id, text, value, decider, version, finding id, group names."""
    mp, salt = {}, 0
    for iid, it in world.items.items():
        if not readable(it.label, p):
            salt += 1
            mp[iid] = "ITEM-mut%04d" % salt
    ren = lambda g: g if g in p.groups else g + "~mut"
    items = {}
    for iid, it in world.items.items():
        if iid in mp:
            lab = Label(it.label.tenants, acl=[[ren(g) for g in c] for c in it.label.acl])
            items[mp[iid]] = Item(mp[iid], it.thread, "req MK-mut%04d title THREADNAME-mut" % salt, "VAL-mut", "DEC-mut", it.version + 7, lab)
        else:
            items[iid] = it
    m = lambda i: mp.get(i, i)
    edges = [(m(a), m(b), t) for a, b, t in world.edges]
    findings = []
    for fid, ins in world.findings:
        fvis = all(readable(world.items[i].label, p) for i in ins)
        findings.append((fid if fvis else "FIND-mut%d" % len(findings), tuple(m(i) for i in ins)))
    return World(items, edges, findings, world.group_of_thread)


def tokens_of(it):
    return [it.id, it.value, it.decider] + [w for w in it.text.split() if w.startswith(("MK-", "THREADNAME-"))]


def check_world(world, mode):
    pol = Policy(1, grants=GRANTS)
    out = {"leaks": 0, "notice_expected": 0, "notice_found": 0, "notice_wrong": 0, "views": 0, "interference": 0, "bytes_checked": 0, "xrefs_visible": 0,
           "finding_hidden": 0, "finding_visible": 0}
    for p in principals(world):
        view = build_view(world, p, pol, mode)
        blob = serialise(view).decode()
        out["views"] += 1
        out["bytes_checked"] += len(blob)
        forbidden = [t for it in world.items.values() if not readable(it.label, p) for t in tokens_of(it)]
        forbidden += [fid for fid, ins in world.findings if not all(readable(world.items[i].label, p) for i in ins)]
        if any(t in blob for t in forbidden):
            out["leaks"] += 1
        for fid, ins in world.findings:
            if all(readable(world.items[i].label, p) for i in ins):
                out["finding_visible"] += 1
            else:
                out["finding_hidden"] += 1
        out["xrefs_visible"] += len(view["xrefs"])
        # notice contract: one per visible item with a conflicts_with edge to an unreadable item, and exactly the contract fields
        expect = set()
        for a, b, t in world.edges:
            if t != "conflicts_with":
                continue
            for l, o in ((a, b), (b, a)):
                if readable(world.items[l].label, p) and not readable(world.items[o].label, p):
                    expect.add(l)
        want = sorted([conflict_notice(l, world.items[l].version) for l in expect], key=lambda n: n["item"])
        got = sorted(view["notices"], key=lambda n: n["item"])
        out["notice_expected"] += len(want)
        out["notice_found"] += len([n for n in got if n in want])
        out["notice_wrong"] += (got != want)
        mv = build_view(mutate(world, p), p, pol, mode)
        out["interference"] += serialise(mv) != serialise(view)
    return out


def run_arm(mode):
    tot = {"worlds": 0, "worlds_with_leak": 0, "worlds_with_cross_conflict": 0, "findings_joining": 0, "views": 0, "leaks": 0, "notice_expected": 0,
           "notice_found": 0, "notice_wrong_views": 0, "interference_views": 0, "bytes_checked": 0, "xrefs_visible": 0, "finding_hidden": 0, "finding_visible": 0,
           "worlds_with_interference": 0}
    for seed in SEEDS:
        for i in range(PER_SEED):
            rng = random.Random(seed * 10000 + i)
            w = gen_world(rng)
            r = check_world(w, mode)
            tot["worlds"] += 1
            tot["worlds_with_leak"] += r["leaks"] > 0
            tot["worlds_with_cross_conflict"] += any(w.items[a].thread != w.items[b].thread and t == "conflicts_with" for a, b, t in w.edges)
            tot["findings_joining"] += len(w.findings)
            tot["worlds_with_interference"] += r["interference"] > 0
            for k in ("views", "leaks", "notice_expected", "notice_found", "bytes_checked", "xrefs_visible", "finding_hidden", "finding_visible"):
                tot[k] += r[k]
            tot["notice_wrong_views"] += r["notice_wrong"]
            tot["interference_views"] += r["interference"]
    return tot


def main():
    arms = {"enforced": run_arm(None)}
    for name, mode in [("ctl_derive_join_dropped", "derive_first"), ("ctl_notice_carries_other_side", "notice_full"), ("ctl_xref_without_decision", "xref_no_check")]:
        arms[name] = run_arm(mode)
    a = arms["enforced"]
    pos = (a["worlds_with_leak"] == 0 and a["notice_wrong_views"] == 0 and a["notice_found"] == a["notice_expected"] and a["interference_views"] == 0
           and a["worlds"] >= 1000 and a["worlds_with_cross_conflict"] >= 0.9 * a["worlds"] and a["findings_joining"] >= a["worlds"] and a["notice_found"] >= 1000
           and a["finding_hidden"] > 0 and a["finding_visible"] > 0 and a["xrefs_visible"] > 0)
    ctl = {}
    for k, v in arms.items():
        if k.startswith("ctl_"):
            rate = v["worlds_with_leak"] / v["worlds"]
            # for the notice control the contract violation is also a failure of the notice check
            ctl[k] = {"leak_rate_worlds": rate, "notice_contract_violations": v["notice_wrong_views"], "interference_views": v["interference_views"], "meets_floor": rate >= 0.10}
    ctl_ok = all(c["meets_floor"] for c in ctl.values())
    verdict = "PASS" if pos and ctl_ok else "FAIL"
    data = {"verdict": verdict, "arms": arms, "controls": ctl, "positive_conditions_met": pos, "controls_meet_floor": ctl_ok,
            "notice_contract": ["kind", "item", "version", "state"],
            "not_proven": "timing and ordering side channels (a notice appearing later than the item), notice frequency over time, and the generic 'context withheld' placeholder "
                          "which tells a viewer that something was withheld; graph shape beyond one hop; real concurrent write paths"}
    write_result("e5_threads", {"seeds": SEEDS, "per_seed": PER_SEED}, data)
    print(verdict, {k: a[k] for k in ("worlds", "worlds_with_leak", "notice_expected", "notice_found", "notice_wrong_views", "interference_views", "worlds_with_cross_conflict", "findings_joining", "finding_hidden", "finding_visible", "xrefs_visible")})
    for k, c in ctl.items():
        print(" ", k, c)


if __name__ == "__main__":
    main()
