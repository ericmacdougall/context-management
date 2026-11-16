"""E1: derived labels are never less restrictive than any input (structural and semantic), with a naive-merge control per dimension.
usage: python -m exp.e1_lattice   (run from policy/)"""
import random
import sys

from exp import common  # noqa: F401  (adds the policy root to sys.path)
from exp.common import write_result
from exp.gen import rand_label, rand_principal, rand_probe, base_policy
from pol.labels import DIMENSIONS, Label, join2, violated_dims
from pol.pdp import decide

N_PER_SEED = 4000
SEEDS = [11, 12, 13, 14, 15]      # fresh seeds for the stamped run; dev tuning used 1..5
PROBES = 40
LAW_TRIPLES = 5000


def naive(dim):
    """join2 with exactly one dimension mishandled."""
    def f(a, b):
        good = join2(a, b)
        kw = dict(tenants=good.tenants, acl=good.acl, regions=good.regions, sensitivity=good.sensitivity, models=good.models,
                  banned=good.banned, local_only=good.local_only, restricted=good.restricted, holds=good.holds, subjects=good.subjects)
        if dim == "tenants":
            kw["tenants"] = a.tenants
        elif dim == "acl":
            kw["acl"] = [set().union(*[set(c) for c in (a.acl | b.acl)])] if (a.acl | b.acl) else []     # groups merged into one OR clause
        elif dim == "regions":
            kw["regions"] = None if (a.regions is None or b.regions is None) else a.regions | b.regions
        elif dim == "sensitivity":
            kw["sensitivity"] = min(a.sensitivity, b.sensitivity)
        elif dim == "models":
            kw["models"] = None if (a.models is None or b.models is None) else a.models | b.models
        elif dim == "banned":
            kw["banned"] = a.banned & b.banned
        elif dim == "local_only":
            kw["local_only"] = a.local_only and b.local_only
        elif dim == "restricted":
            kw["restricted"] = a.restricted and b.restricted
        elif dim == "holds":
            kw["holds"] = a.holds & b.holds
        elif dim == "subjects":
            kw["subjects"] = a.subjects
        return Label(**kw)
    return f


def derive_tree(rng, joiner):
    leaves = [rand_label(rng) for _ in range(rng.randint(1, 5))]
    cur = leaves[0]
    for l in leaves[1:]:
        cur = joiner(cur, l)
    depth = rng.randint(1, 3)
    for _ in range(depth - 1):                       # derive again from the derived record plus fresh inputs
        extra = [rand_label(rng) for _ in range(rng.randint(0, 2))]
        for l in extra:
            cur = joiner(cur, l)
        leaves += extra
    return cur, leaves


def run(seed, n, joiner):
    rng = random.Random(seed)
    pol = base_policy()
    st = {"derivations": 0, "multi_input": 0, "structural_viol": 0, "semantic_viol_derivations": 0, "probes": 0, "probe_allowed": 0,
          "probe_denied": 0, "viol_dims": {}, "first_counterexample": None}
    for _ in range(n):
        d, leaves = derive_tree(rng, joiner)
        st["derivations"] += 1
        st["multi_input"] += len(leaves) >= 2
        bad = set()
        for l in leaves:  # single-input derivations are checked too (the derived label must equal the input)
            bad.update(violated_dims(l, d))
        if bad:
            st["structural_viol"] += 1
            for b in bad:
                st["viol_dims"][b] = st["viol_dims"].get(b, 0) + 1
        sem = False
        for _p in range(PROBES):
            pr = rand_principal(rng)
            action, ctx = rand_probe(rng, pol)
            da = decide(pr, action, d, ctx, policy=pol).allow
            st["probes"] += 1
            st["probe_allowed" if da else "probe_denied"] += 1
            if da:
                for l in leaves:
                    if not decide(pr, action, l, ctx, policy=pol).allow:
                        sem = True
                        if st["first_counterexample"] is None:
                            st["first_counterexample"] = {"action": action, "dims": sorted(violated_dims(l, d))}
                        break
        # erasure semantics: whoever is erased must be reachable from every derived record that was computed from a record mentioning them
        for subj in ("s1", "s2", "s3"):
            if any(subj in l.subjects for l in leaves) and subj not in d.subjects:
                sem = True
                if st["first_counterexample"] is None:
                    st["first_counterexample"] = {"action": "erase_subject", "dims": ["subjects"]}
        st["semantic_viol_derivations"] += sem
    return st


def laws(rng, n):
    bad = {"commutative": 0, "associative": 0, "idempotent": 0}
    for _ in range(n):
        a, b, c = rand_label(rng), rand_label(rng), rand_label(rng)
        bad["commutative"] += join2(a, b) != join2(b, a)
        bad["associative"] += join2(join2(a, b), c) != join2(a, join2(b, c))
        bad["idempotent"] += join2(a, a) != a
    return bad


def main():
    params = {"n_per_seed": N_PER_SEED, "seeds": SEEDS, "probes_per_derivation": PROBES, "law_triples": LAW_TRIPLES}
    good = {s: run(s, N_PER_SEED, join2) for s in SEEDS}
    tot = {k: sum(good[s][k] for s in SEEDS) for k in ("derivations", "multi_input", "structural_viol", "semantic_viol_derivations", "probes", "probe_allowed", "probe_denied")}
    lw = laws(random.Random(99), LAW_TRIPLES)
    ok = (tot["structural_viol"] == 0 and tot["semantic_viol_derivations"] == 0 and sum(lw.values()) == 0 and tot["derivations"] >= 20000
          and tot["probes"] >= 200000 and tot["probe_allowed"] >= 0.05 * tot["probes"] and tot["probe_denied"] >= 0.05 * tot["probes"])
    controls = {}
    for dim in DIMENSIONS:
        runs = [run(sd, N_PER_SEED, naive(dim)) for sd in SEEDS]
        sfs = [r["structural_viol"] / max(1, r["multi_input"]) for r in runs]
        sms = [r["semantic_viol_derivations"] / max(1, r["multi_input"]) for r in runs]
        only = all(set(r["viol_dims"]) == {dim} for r in runs)
        sf, sm = min(sfs), min(sms)          # worst seed (proof rule 7)
        controls[dim] = {"structural_rate_worst_seed": sf, "semantic_rate_worst_seed": sm, "per_seed_structural": sfs, "per_seed_semantic": sms,
                         "reason_is_dropped_dimension": only, "meets_floor": sf >= 0.10 and sm >= 0.05 and only, "first_counterexample": runs[0]["first_counterexample"]}
    ok_ctl = all(c["meets_floor"] for c in controls.values())
    verdict = "PASS" if ok and ok_ctl else "FAIL"
    data = {"verdict": verdict, "per_seed": good, "totals": tot, "lattice_law_failures": lw, "controls": controls, "pass_conditions_met": ok,
            "controls_meet_floor": ok_ctl, "not_proven": "label generator universe is small (4 groups, 3 regions, 4 models, 2 tenants); decide() rules not proven complete, only monotone under join"}
    write_result("e1_lattice", params, data)
    print(verdict, tot, lw)
    for d, c in controls.items():
        print(" control", d, "structural %.2f semantic %.2f only=%s floor=%s" % (c["structural_rate_worst_seed"], c["semantic_rate_worst_seed"], c["reason_is_dropped_dimension"], c["meets_floor"]))


if __name__ == "__main__":
    main()
