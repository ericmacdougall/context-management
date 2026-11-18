"""Independent recomputation of E1 from its raw rows (stdlib only; imports nothing from pol or exp).

Rows: arm in {enforced, ctl_<dim>, laws, roundtrip}. Labels are lists in the order
tenants, acl, regions, sensitivity, models, banned, local_only, restricted, holds, subjects.

Evidence identity (GoodQA #16096 class B, verify/evidence.py): the design parameters in params must equal PINS (THRESHOLDS E1), the seeds
are params.seeds (at least MIN_SEEDS, distinct). The rows must be EXACTLY: one derivation per (arm, seed, n) for the arms enforced and
ctl_<dim> and n = 0..n_per_seed-1; one law triple per ("laws", seed, n), n = 0..law_triples/5-1 (exp/e1_lattice.py writes LAW_TRIPLES // 5
per seed); one round trip per ("roundtrip", seed, n), n = 0..roundtrip_per_seed-1. A duplicate, missing, extra, unidentified or
unclassified row FAILs before any counter is read."""
import importlib.util
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()
DIMS = ["tenants", "acl", "regions", "sensitivity", "models", "banned", "local_only", "restricted", "holds", "subjects"]
# the design (THRESHOLDS E1; exp/e1_lattice.py): pinned here, never read from the judged document
PINS = {"n_per_seed": 4000, "probes_per_derivation": 40, "law_triples": 5000, "roundtrip_per_seed": 1000}
MIN_SEEDS = 5
ARMS = ["enforced"] + ["ctl_" + d for d in DIMS]


def _s(x):
    return None if x is None else frozenset(x)


def _acl(x):
    return frozenset(frozenset(c) for c in x)


def dim_ok(dim, a, b):
    """Is the derived value b at least as restrictive as the input value a in this dimension (own definition)?"""
    if dim in ("tenants", "banned", "holds", "subjects"):
        return set(a) <= set(b)
    if dim == "acl":
        # every input clause must be implied by some derived clause (a derived clause that is a subset is stricter)
        return all(any(_s(cb) <= _s(ca) for cb in b) for ca in a)
    if dim in ("regions", "models"):
        return a is None or (b is not None and set(b) <= set(a))
    if dim == "sensitivity":
        return a <= b
    if dim in ("local_only", "restricted"):
        return (not a) or b
    raise ValueError(dim)


def bad_dims(leaf, derived):
    return {d for k, d in enumerate(DIMS) if not dim_ok(d, leaf[k], derived[k])}


def own_join(dicts):
    """Own join of label dicts, canonical form as stored."""
    t, ban, hold, sub, cl = set(), set(), set(), set(), set()
    reg = mod = None
    sens, lo, rs = 0, False, False
    for d in dicts:
        t |= set(d["tenants"])
        ban |= set(d["banned"])
        hold |= set(d["holds"])
        sub |= set(d["subjects"])
        cl |= {frozenset(c) for c in d["acl"]}
        if d["regions"] is not None:
            reg = set(d["regions"]) if reg is None else reg & set(d["regions"])
        if d["models"] is not None:
            mod = set(d["models"]) if mod is None else mod & set(d["models"])
        sens = max(sens, d["sensitivity"])
        lo = lo or d["local_only"]
        rs = rs or d["restricted"]
    cl = {c for c in cl if not any(o < c for o in cl)}
    return {"tenants": sorted(t), "acl": sorted(sorted(c) for c in cl), "regions": None if reg is None else sorted(reg), "sensitivity": sens,
            "models": None if mod is None else sorted(mod), "banned": sorted(ban), "local_only": lo, "restricted": rs, "holds": sorted(hold),
            "subjects": sorted(sub)}


def _canon(lab):
    """Order-insensitive comparison form of an encoded label list."""
    out = []
    for k, v in zip(DIMS, lab):
        if k == "acl":
            out.append(_acl(v))
        elif isinstance(v, list):
            out.append(frozenset(v))
        else:
            out.append(v)
    return tuple(out)


def _family(r):
    a = r["arm"]
    if a in ("laws", "roundtrip"):
        return a
    return "derivation" if a == "enforced" or (isinstance(a, str) and a.startswith("ctl_")) else None


def _ident(r):
    return r["arm"], r["seed"], r["n"]


def verify(result_doc, rows):
    g = EV.Gate("e1_lattice", result_doc)
    g.pinned(PINS)
    sd = g.seeds(MIN_SEEDS)
    fam = g.split(rows, _family)
    g.family("derivation", fam.get("derivation", []), _ident, {EV.key(a, s, n) for a in ARMS for s in sd for n in range(PINS["n_per_seed"])})
    g.family("laws", fam.get("laws", []), _ident, {EV.key("laws", s, n) for s in sd for n in range(PINS["law_triples"] // 5)})
    g.family("roundtrip", fam.get("roundtrip", []), _ident, {EV.key("roundtrip", s, n) for s in sd for n in range(PINS["roundtrip_per_seed"])})
    if not g.ok:
        return g.fail()
    probes_per = PINS["probes_per_derivation"]
    arms = {}
    for r in rows:
        arms.setdefault(r["arm"], []).append(r)
    checks = {"evidence": g.report()}
    per = {}
    for arm, rs in arms.items():
        if arm in ("laws", "roundtrip"):
            continue
        for r in rs:
            s = per.setdefault((arm, r["seed"]), {"derivations": 0, "multi": 0, "structural_multi": 0, "semantic_multi": 0, "structural": 0,
                                                   "semantic": 0, "probes": 0, "allowed": 0, "dims": set(), "bit_errors": 0})
            bad = set()
            for l in r["leaves"]:
                bad |= bad_dims(l, r["derived"])
            # semantic: a probe the derived label allowed that some input did not
            if len(r["d"]) != probes_per or len(r["i"]) != probes_per:
                s["bit_errors"] += 1
            sem = any(dc == "1" and ic == "0" for dc, ic in zip(r["d"], r["i"]))
            sem = sem or bool(set().union(*[set(l[9]) for l in r["leaves"]]) - set(r["derived"][9]))     # subject coverage (erasure scope)
            s["bit_errors"] += any((dc == "1") != (ic != "-") for dc, ic in zip(r["d"], r["i"]))
            multi = len(r["leaves"]) >= 2
            s["derivations"] += 1
            s["multi"] += multi
            s["structural"] += bool(bad)
            s["semantic"] += sem
            s["structural_multi"] += bool(bad) and multi
            s["semantic_multi"] += sem and multi
            s["probes"] += len(r["d"])
            s["allowed"] += r["d"].count("1")
            s["dims"] |= bad
    en = [v for (a, _sd), v in per.items() if a == "enforced"]
    tot = {k: sum(v[k] for v in en) for k in ("derivations", "structural", "semantic", "probes", "allowed", "bit_errors")}
    checks["enforced"] = tot
    ok = (tot["structural"] == 0 and tot["semantic"] == 0 and tot["bit_errors"] == 0 and tot["derivations"] >= 20000 and tot["probes"] >= 800000
          and 0.05 * tot["probes"] <= tot["allowed"] <= 0.95 * tot["probes"])
    # lattice laws from the recorded joins
    lf = 0
    for r in arms.get("laws", []):
        lf += _canon(r["ab"]) != _canon(r["ba"]) or _canon(r["ab_c"]) != _canon(r["a_bc"]) or _canon(r["aa"]) != _canon(r["a"])
        # and each recorded join is the own join of its operands
        enc2d = lambda x: dict(zip(DIMS, x))  # noqa: E731
        lf += _canon([own_join([enc2d(r["a"]), enc2d(r["b"])])[k] for k in DIMS]) != _canon(r["ab"])
    checks["law_failures"] = lf
    checks["law_triples"] = len(arms.get("laws", []))
    ok = ok and lf == 0 and checks["law_triples"] * 5 >= PINS["law_triples"]
    # round trip
    mism, stored = 0, 0
    for r in arms.get("roundtrip", []):
        mism += r["dict_before"] != r["dict_after"] or r["digest_before"] != r["digest_after"] or not all(r["eq_after"])
        if r["stored_derived"] is not None:
            stored += 1
            mism += r["stored_derived"] != own_join(r["lineage"])
    checks["roundtrip"] = {"mismatch": mism, "stored_derived": stored, "rows": len(arms.get("roundtrip", []))}
    ok = ok and mism == 0 and stored >= 2000
    # controls: worst seed must meet the floors, and the violated dimension is exactly the dropped one
    ctl = {}
    for (arm, sd), v in per.items():
        if not arm.startswith("ctl_"):
            continue
        c = ctl.setdefault(arm[4:], {"structural_worst_seed": 1.0, "semantic_worst_seed": 1.0, "dims": set()})
        c["structural_worst_seed"] = min(c["structural_worst_seed"], v["structural_multi"] / max(1, v["multi"]))
        c["semantic_worst_seed"] = min(c["semantic_worst_seed"], v["semantic_multi"] / max(1, v["multi"]))
        c["dims"] |= v["dims"]
    ok_ctl = len(ctl) == len(DIMS)
    for dim, c in ctl.items():
        c["reason_is_dropped_dimension"] = c["dims"] == {dim}
        c["dims"] = sorted(c["dims"])
        c["meets_floor"] = c["structural_worst_seed"] >= 0.10 and c["semantic_worst_seed"] >= 0.05 and c["reason_is_dropped_dimension"]
        c["structural_worst_seed"] = round(c["structural_worst_seed"], 4)
        c["semantic_worst_seed"] = round(c["semantic_worst_seed"], 4)
        ok_ctl = ok_ctl and c["meets_floor"]
    checks["controls"] = ctl
    return {"verdict": "PASS" if ok and ok_ctl else "FAIL", "checks": checks}
