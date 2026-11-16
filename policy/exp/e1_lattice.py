"""E1: a derived label is never less restrictive than any input (structural and semantic). Controls are ten broken
implementations of pol.labels.join2 (one dimension merged naively each), built as one-patch source variants (exp.patches.variant).
A round-trip arm checks that labels survive dict/JSON round-trips and that Store.append_derived stores exactly the join of the lineage.

Per-item rows hold RAW observations only: input labels and the derived label (as lists in DIMENSIONS order), and per probe a character
in `d` (derived allowed 1/0) and `i` (when derived allowed: every input allowed 1/0, else '-'). The experiment's own summary uses the
production lattice (pol.labels.violated_dims); verify/e1.py recomputes every verdict from the rows with its own code.
usage: python -m exp.e1_lattice   (run from policy/; seeds from exp.seeds; E1_PROCS worker processes)"""
import json
import multiprocessing as mp
import os
import random
import time

from exp import common  # noqa: F401  (adds the policy root to sys.path)
from exp.common import write_items, write_result
from exp.patches import variant
from exp.seeds import seeds
from pol import _pdp
from pol import labels as LAB
from pol.directory import Directory, Principal, SessionAuthority
from pol.enforce import AuditLog, Enforcer, PolicyDenied, Store
from pol.labels import DIMENSIONS, Label, violated_dims
from pol.policy import ACTIONS, Dest, Policy

N_PER_SEED = int(os.environ.get("E1_N_PER_SEED", "4000"))
PROBES = int(os.environ.get("E1_PROBES", "40"))
LAW_TRIPLES = 5000
RT_PER_SEED = int(os.environ.get("E1_RT_PER_SEED", "1000"))
NOW = 10.0
GROUPS = ["g1", "g2", "g3", "g4"]
REGIONS = ["eu", "us", "apac"]
MODELS = ["m1", "m2", "m3", "m4"]
DIMS = list(DIMENSIONS)

# one-patch controls of pol/labels.py join2: (old fragment, naive replacement)
NAIVE = {
    "tenants": ("tenants=a.tenants | b.tenants", "tenants=a.tenants"),
    "acl": ("acl=a.acl | b.acl", "acl=(frozenset([frozenset().union(*(a.acl | b.acl))]) if (a.acl | b.acl) else frozenset())"),
    "regions": ("regions=_inter(a.regions, b.regions)", "regions=(None if (a.regions is None or b.regions is None) else a.regions | b.regions)"),
    "sensitivity": ("sensitivity=max(a.sensitivity, b.sensitivity)", "sensitivity=min(a.sensitivity, b.sensitivity)"),
    "models": ("models=_inter(a.models, b.models)", "models=(None if (a.models is None or b.models is None) else a.models | b.models)"),
    "banned": ("banned=a.banned | b.banned", "banned=a.banned & b.banned"),
    "local_only": ("local_only=a.local_only or b.local_only", "local_only=a.local_only and b.local_only"),
    "restricted": ("restricted=a.restricted or b.restricted", "restricted=a.restricted and b.restricted"),
    "holds": ("holds=a.holds | b.holds", "holds=a.holds & b.holds"),
    "subjects": ("subjects=a.subjects | b.subjects", "subjects=a.subjects"),
}


def joiner(arm):
    if arm == "enforced":
        return LAB.join2
    old, new = NAIVE[arm[4:]]
    m = variant("pol.labels", old, new, "e1_naive_" + arm[4:])
    return lambda a, b: Label.from_dict(m.join2(m.Label.from_dict(a.to_dict()), m.Label.from_dict(b.to_dict())).to_dict())


def enc(l):
    d = l.to_dict()
    return [d[k] for k in DIMS]


def rand_label(rng, permissive=0.65):
    tenants = ["t1"] if rng.random() < 0.85 else ["t1", "t2"]
    acl = [rng.sample(GROUPS, rng.choice([1, 2, 3])) for _ in range(rng.choice([0, 0, 0, 1, 1, 2]))]
    regions = None if rng.random() < permissive else rng.sample(REGIONS, rng.choice([1, 2]))
    models = None if rng.random() < permissive else rng.sample(MODELS, rng.choice([1, 2, 3]))
    banned = [] if rng.random() < 0.6 else rng.sample(MODELS, rng.choice([1, 2]))
    return Label(tenants=tenants, acl=acl, regions=regions, sensitivity=rng.choice([0, 0, 0, 1, 1, 2, 3]), models=models, banned=banned,
                 local_only=rng.random() < 0.25, restricted=rng.random() < 0.08,
                 holds=[] if rng.random() < 0.7 else ["h1"], subjects=[] if rng.random() < 0.7 else rng.sample(["s1", "s2", "s3"], rng.choice([1, 2])))


def rand_principal(rng, tenant="t1", pid=None):
    return Principal(pid or ("p1" if rng.random() < 0.15 else "p%d" % rng.randrange(10 ** 6)), tenant, frozenset(rng.sample(GROUPS, rng.choice([2, 3, 4, 4]))),
                     rng.choice([2, 3, 3, 3]), rng.random() < 0.7, rng.choice(["human", "agent", "analyst"]),
                     rng.choice(REGIONS + [None]), rng.choice(["d_model_eu", "d_model_us", None]))


def base_policy():
    T = ["t1", "t2"]
    dests = {
        "d_model_eu": Dest("model", "eu", True, 3, "m1", None, T),
        "d_model_eu_cloud": Dest("model", "eu", False, 3, "m2", None, T),
        "d_model_us": Dest("model", "us", False, 2, "m3", None, T),
        "d_model_cyber": Dest("model", "us", False, 3, "m4", "cyber", T),
        "d_embed_eu": Dest("embedder", "eu", True, 3, "m1", None, T),
        "d_embed_us": Dest("embedder", "us", False, 2, "m3", None, T),
        "d_export_eu": Dest("export", "eu", True, 3, None, None, T),
        "d_export_us": Dest("export", "us", False, 1, None, None, T),
    }
    return Policy(1, grants={t: frozenset(ACTIONS) for t in T}, hold_events=frozenset(["h1"]), destinations=dests,
                  models={m: {"family": m, "version": "1"} for m in MODELS},
                  authorizations={"auth1": {"tenant": "t1", "programme": "cyber", "expires": 1000, "principal": "p1"}})


def rand_probe(rng, policy):
    action = rng.choice(ACTIONS) if rng.random() < 0.5 else rng.choice(["send_model", "embed", "send_inbox", "export", "send_model", "erase"])
    req = {}
    a = rng.choice(["auth1", None, "zzz"])
    if a:
        req["authorization"] = a
    rcp = None
    if action in ("send_model", "embed", "export") or (action == "inject" and rng.random() < 0.7):
        kind = {"send_model": "model", "embed": "embedder", "export": "export", "inject": "model"}[action]
        names = sorted(n for n, d in policy.destinations.items() if d.kind == kind)
        req["destination"] = rng.choice(names + ["unknown_dest"] if rng.random() < 0.05 else names)
    if action == "send_inbox":
        rcp = rand_principal(rng, pid="rcp%d" % rng.randrange(1000))
    return action, req, rcp


def derive_tree(rng, j):
    leaves = [rand_label(rng) for _ in range(rng.randint(1, 5))]
    cur = leaves[0]
    for l in leaves[1:]:
        cur = j(cur, l)
    for _ in range(rng.randint(1, 3) - 1):              # derive again from the derived record plus fresh inputs
        extra = [rand_label(rng) for _ in range(rng.randint(0, 2))]
        for l in extra:
            cur = j(cur, l)
        leaves += extra
    return cur, leaves


def run(job):
    arm, seed = job
    j = joiner(arm)
    rng = random.Random("%s|lattice" % seed)             # the same derivations and probes for every arm of a seed
    pol = base_policy()
    rows, summ = [], {"arm": arm, "seed": seed, "derivations": 0, "multi": 0, "structural": 0, "structural_multi": 0, "semantic_multi": 0,
                      "semantic": 0, "probes": 0, "allowed": 0, "dims": {}}
    for n in range(N_PER_SEED):
        d, leaves = derive_tree(rng, j)
        dbits, ibits = [], []
        for _ in range(PROBES):
            pr = rand_principal(rng)
            action, req, rcp = rand_probe(rng, pol)
            da = _pdp.evaluate(pr, action, [d], req, pol, NOW, rcp).allow
            dbits.append("1" if da else "0")
            ibits.append(("1" if all(_pdp.evaluate(pr, action, [l], req, pol, NOW, rcp).allow for l in leaves) else "0") if da else "-")
        rows.append({"arm": arm, "seed": seed, "n": n, "leaves": [enc(l) for l in leaves], "derived": enc(d), "d": "".join(dbits), "i": "".join(ibits)})
        bad = set()
        for l in leaves:
            bad.update(violated_dims(l, d))
        sem = "0" in ibits or bool(set().union(*[l.subjects for l in leaves]) - d.subjects)    # erasure scope: every subject reaches the derived record
        multi = len(leaves) >= 2
        summ["derivations"] += 1
        summ["multi"] += multi
        summ["structural"] += bool(bad)
        summ["structural_multi"] += bool(bad) and multi
        summ["semantic"] += sem
        summ["semantic_multi"] += sem and multi
        summ["probes"] += PROBES
        summ["allowed"] += dbits.count("1")
        for b in bad:
            summ["dims"][b] = summ["dims"].get(b, 0) + 1
    return rows, summ


def laws(seed):
    rng = random.Random("%s|laws" % seed)
    rows, bad = [], 0
    J = LAB.join2
    for k in range(LAW_TRIPLES // 5):
        a, b, c = rand_label(rng), rand_label(rng), rand_label(rng)
        r = {"arm": "laws", "seed": seed, "n": k, "a": enc(a), "b": enc(b), "c": enc(c), "ab": enc(J(a, b)), "ba": enc(J(b, a)),
             "ab_c": enc(J(J(a, b), c)), "a_bc": enc(J(a, J(b, c))), "aa": enc(J(a, a))}
        bad += r["ab"] != r["ba"] or r["ab_c"] != r["a_bc"] or r["aa"] != r["a"]
        rows.append(r)
    return rows, {"law_failures": bad}


def roundtrip(seed):
    """Labels survive to_dict/JSON/from_dict and digest; Store.append_derived stores the join of its lineage (read back via the store)."""
    rng = random.Random("%s|roundtrip" % seed)
    me = Principal("rt", "t1", frozenset(GROUPS), 3, True, "human", "eu", None)
    d = Directory([me])
    sa = SessionAuthority(d)
    pol = base_policy()
    enf = Enforcer(lambda: pol, d, sa, AuditLog())
    store = Store(enf)
    s = sa.issue("rt", "owner_ui")
    rows, bad, stored = [], 0, 0
    for k in range(RT_PER_SEED):
        leaves = [rand_label(rng, permissive=0.9) for _ in range(rng.randint(1, 4))]
        ids, stored_leaves, refused = [], [], 0
        for l in leaves:
            try:
                ids.append(store.append(s, "x", l))
                stored_leaves.append(l)
            except PolicyDenied:
                refused += 1
        after = [Label.from_dict(json.loads(json.dumps(l.to_dict()))) for l in leaves]
        row = {"arm": "roundtrip", "seed": seed, "n": k, "dict_before": [l.to_dict() for l in leaves], "dict_after": [a.to_dict() for a in after],
               "eq_after": [a == l for a, l in zip(after, leaves)], "digest_before": [l.digest() for l in leaves],
               "digest_after": [a.digest() for a in after], "lineage": [l.to_dict() for l in stored_leaves], "append_refused": refused,
               "stored_derived": None}
        if ids:
            try:
                rid = store.append_derived(s, "derived", ids)
                row["stored_derived"] = store.read(s, rid).label.to_dict()
                stored += 1
                bad += row["stored_derived"] != LAB.join(stored_leaves).to_dict()
            except PolicyDenied as e:
                row["derived_refused"] = e.decision.reason
        bad += not all(row["eq_after"]) or row["digest_before"] != row["digest_after"] or row["dict_before"] != row["dict_after"]
        rows.append(row)
    return rows, {"roundtrip_mismatch": bad, "stored_derived": stored}


def main():
    sd = seeds("e1")
    arms = ["enforced"] + ["ctl_" + k for k in DIMS]
    t0 = time.time()
    with mp.Pool(int(os.environ.get("E1_PROCS", "16"))) as pool:
        parts = pool.map(run, [(a, s) for a in arms for s in sd])
        lw = pool.map(laws, sd)
        rt = pool.map(roundtrip, sd)
    rows = [r for p, _s in parts + lw + rt for r in p]
    summ = {}
    for _p, s in parts:
        summ.setdefault(s["arm"], []).append(s)
    en = summ["enforced"]
    tot = {k: sum(s[k] for s in en) for k in ("derivations", "multi", "structural", "semantic", "probes", "allowed")}
    lawf = sum(s["law_failures"] for _r, s in lw)
    rtm = {"mismatch": sum(s["roundtrip_mismatch"] for _r, s in rt), "stored_derived": sum(s["stored_derived"] for _r, s in rt)}
    ok = (tot["structural"] == 0 and tot["semantic"] == 0 and lawf == 0 and tot["derivations"] >= 20000 and tot["probes"] >= 800000
          and 0.05 * tot["probes"] <= tot["allowed"] <= 0.95 * tot["probes"] and rtm["mismatch"] == 0 and rtm["stored_derived"] >= 2000)
    controls = {}
    for dim in DIMS:
        ss = summ["ctl_" + dim]
        sf = min(s["structural_multi"] / max(1, s["multi"]) for s in ss)
        sm = min(s["semantic_multi"] / max(1, s["multi"]) for s in ss)
        only = all(set(s["dims"]) == {dim} for s in ss)
        controls[dim] = {"structural_worst_seed": round(sf, 4), "semantic_worst_seed": round(sm, 4), "reason_is_dropped_dimension": only,
                         "meets_floor": sf >= 0.10 and sm >= 0.05 and only}
    ok_ctl = all(c["meets_floor"] for c in controls.values())
    params = {"seeds": sd, "n_per_seed": N_PER_SEED, "probes_per_derivation": PROBES, "law_triples": LAW_TRIPLES, "roundtrip_per_seed": RT_PER_SEED,
              "controls": {k: {"module": "pol.labels", "old": o, "new": n} for k, (o, n) in NAIVE.items()}}
    fn, h = write_items("e1_lattice", rows)
    size = os.path.getsize(os.path.join(common.RESULTS, fn))
    data = {"verdict": "PASS" if ok and ok_ctl else "FAIL", "enforced_totals": tot, "law_failures": lawf, "roundtrip": rtm, "controls": controls,
            "pass_conditions_met": ok, "controls_meet_floor": ok_ctl, "items": {"file": fn, "sha256": h, "bytes": size},
            "runtime_s": round(time.time() - t0, 1),
            "not_proven": "label universe is small (4 groups, 3 regions, 4 models, 2 tenants); decide() monotone under join, not proven complete"}
    write_result("e1_lattice", params, data)
    print(data["verdict"], tot, "laws", lawf, "roundtrip", rtm, "artifact_bytes", size, "runtime_s", data["runtime_s"])
    for d, c in controls.items():
        print(" control", d, c)


if __name__ == "__main__":
    main()
