"""Evidence identity properties of verify/e1.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E1 and the
E1 sections of the amendments, exp/e1_lattice.py): arms enforced and ctl_<dim> for the ten dimensions, n = 0..3999 derivations per arm and
seed (5 seeds, 40 probes each); law triples k = 0..999 per seed (5000 in all, LAW_TRIPLES // 5 per seed); round trips k = 0..999 per
seed. Identity: (arm, seed, n). The rows here are minimal but valid: every derivation has the same two leaves, so the suite stays a few
seconds long."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS = [71, 72, 73, 74, 75]
N, PROBES, LAWS, RT = 4000, 40, 5000, 1000
DIMS = ["tenants", "acl", "regions", "sensitivity", "models", "banned", "local_only", "restricted", "holds", "subjects"]
LEAF = [["t1"], [["g1"]], ["eu"], 1, ["m1"], ["m9"], True, True, ["h1"], ["s1"]]
LEAF_D = dict(zip(DIMS, LEAF))
LOOSER = {"tenants": [], "acl": [["g1", "g2"]], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False,
          "restricted": False, "holds": [], "subjects": []}
DOC = {"name": "e1_lattice", "params": {"seeds": SEEDS, "n_per_seed": N, "probes_per_derivation": PROBES, "law_triples": LAWS,
                                         "roundtrip_per_seed": RT}, "result": {"verdict": "PASS"}}
D_BITS, I_OK, I_SEM = "10" * (PROBES // 2), "1-" * (PROBES // 2), "0-" + "1-" * (PROBES // 2 - 1)


def _derived(arm):
    if arm == "enforced":
        return LEAF
    d = arm[4:]
    return [LOOSER[d] if k == d else v for k, v in zip(DIMS, LEAF)]


def _deriv(arm, seed, n):
    return {"arm": arm, "seed": seed, "n": n, "leaves": [LEAF, LEAF], "derived": _derived(arm), "d": D_BITS,
            "i": I_OK if arm == "enforced" else I_SEM}


def honest():
    rows = [_deriv(a, s, n) for a in ["enforced"] + ["ctl_" + d for d in DIMS] for s in SEEDS for n in range(N)]
    rows += [{"arm": "laws", "seed": s, "n": k, "a": LEAF, "b": LEAF, "c": LEAF, "ab": LEAF, "ba": LEAF, "ab_c": LEAF, "a_bc": LEAF, "aa": LEAF}
             for s in SEEDS for k in range(LAWS // 5)]
    stored = {k: (sorted(v) if isinstance(v, list) and k != "acl" else v) for k, v in LEAF_D.items()}
    rows += [{"arm": "roundtrip", "seed": s, "n": k, "dict_before": [LEAF_D], "dict_after": [LEAF_D], "eq_after": [True], "digest_before": ["x"],
              "digest_after": ["x"], "lineage": [LEAF_D], "append_refused": 0, "stored_derived": stored} for s in SEEDS for k in range(RT)]
    return rows


class E1Evidence(Props, unittest.TestCase):
    V = load("e1")
    FAMILIES = (Fam("derivation", lambda r: r["arm"] not in ("laws", "roundtrip"), ["arm", "seed", "n"], index="n", seed="seed", arm="arm",
                    end=lambda s, r: N),
                Fam("laws", lambda r: r["arm"] == "laws", ["arm", "seed", "n"], index="n", seed="seed", arm="arm", end=lambda s, r: LAWS // 5),
                Fam("roundtrip", lambda r: r["arm"] == "roundtrip", ["arm", "seed", "n"], index="n", seed="seed", arm="arm", end=lambda s, r: RT))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def sample(self, fam):
        rows = self.rows()
        if fam.name == "derivation":       # an enforced derivation (break_row needs one), n = 2000 on the first seed
            return 2000, 2001
        return Props.sample(self, fam)

    def break_row(self, fam, row):
        if fam.name == "derivation":
            return dict(row, i=I_SEM)
        if fam.name == "laws":
            return dict(row, ab=[[]] + LEAF[1:])          # a ⊔ b no longer equals b ⊔ a
        return dict(row, eq_after=[False])

    def shrunk(self):
        ten = [71, 72, 73, 74, 75, 76, 77, 78, 79, 80]
        half = dict(DOC, params=dict(DOC["params"], seeds=ten, n_per_seed=N // 2))
        rows = [r for r in honest() if r["arm"] in ("laws", "roundtrip")]
        rows += [_deriv(a, s, n) for a in ["enforced"] + ["ctl_" + d for d in DIMS] for s in ten for n in range(N // 2)]
        laws = dict(DOC, params=dict(DOC["params"], law_triples=1000))
        lrows = [r for r in honest() if r["arm"] != "laws" or r["n"] < 40]
        return [("n_per_seed 2000 over ten seeds declared and run", half, rows), ("law_triples = 1000 declared, 200 law rows", laws, lrows)]


if __name__ == "__main__":
    unittest.main()
