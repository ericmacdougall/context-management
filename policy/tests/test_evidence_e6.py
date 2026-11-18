"""Evidence identity properties of verify/e6.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E6,
exp/e6_policy_format.py): per seed, empty i = 0..2999, malformed i = 0..2999 (5 probes each, safe and lenient control), valid i = 0..2999;
one example row, five hold_pdp rows (one per case), one hold_join row and one erase_service row. Identity: (arm, seed, i) for the indexed
arms (the "i" field is new in exp/e6_policy_format.py: the stamped rows lack it and the next stamped run must regenerate them),
(arm, seed, case) for hold_pdp, (arm, seed) for the rest. Two seeds here so a seed relabel is exercised (THRESHOLDS stamps one)."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS = [929, 930]
N = 3000
DOC = {"name": "e6_policy_format", "params": {"seeds": SEEDS, "malformed_per_seed": N, "probes_per_doc": 5, "empty_per_seed": N,
                                               "valid_per_seed": N}, "result": {"verdict": "PASS"}}
DENY = [False, "deny", "read"]
HOLDS = {"recorded_hold": [False, "erasure_queued", "erase"], "unrecorded_hold": [True, None, "erase"], "no_hold": [True, None, "erase"],
         "hold_event_removed": [True, None, "erase"], "other_tenant": [False, "tenant", "erase"]}
BEFORE = ["alice-about-dana", "alice-about-dana-held", "bob-about-dana", "bob-about-erin"]
INDEXED = ("empty", "malformed", "valid")


def honest():
    rows = []
    for s in SEEDS:
        rows += [{"arm": "empty", "seed": s, "returned": {"Policy(1)": DENY, "DENY_ALL": DENY, "safe({})": DENY, "safe(None)": DENY}, "i": i}
                 for i in range(N)]
        rows += [{"arm": "malformed", "seed": s, "kind": "drop_key", "doc": "{}", "parse": "PolicyError", "safe": [DENY] * 5,
                  "lenient_ctl": [[True, None, "read"]] + [DENY] * 4, "i": i} for i in range(N)]
        rows += [{"arm": "valid", "seed": s, "returned": [True, None, "read"] if i % 2 else DENY, "i": i} for i in range(N)]
        rows.append({"arm": "example", "seed": s, "example_flag": True, "example_tenants": ["t1"]})
        rows += [{"arm": "hold_pdp", "seed": s, "case": c, "returned": v} for c, v in HOLDS.items()]
        rows.append({"arm": "hold_join", "seed": s, "inputs": [{"holds": ["h1"], "subjects": ["s1"]}, {"holds": [], "subjects": ["s2"]}],
                     "derived": {"holds": ["h1"], "subjects": ["s1", "s2"]}})
        rows.append({"arm": "erase_service", "seed": s, "before": BEFORE, "after": ["alice-about-dana-held", "bob-about-erin"],
                     "non_admin": {"refused": "unauthorized"}, "admin": {"erased": 2, "queued": 1, "denied": 0, "refused": None}})
    return rows


def _fam(arm):
    return Fam(arm, lambda r: r["arm"] == arm, ["arm", "seed", "i"], index="i", seed="seed", arm="arm", end=lambda s, r: N)


class E6Evidence(Props, unittest.TestCase):
    V = load("e6")
    FAMILIES = (_fam("empty"), _fam("malformed"), _fam("valid"),
                Fam("hold_pdp", lambda r: r["arm"] == "hold_pdp", ["arm", "seed", "case"], seed="seed", arm="arm"),
                Fam("single", lambda r: r["arm"] in ("example", "hold_join", "erase_service"), ["arm", "seed"], seed="seed", arm="arm"))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def break_row(self, fam, row):
        if fam.name == "empty":
            return dict(row, returned=dict(row["returned"], DENY_ALL=[True, None, "read"]))
        if fam.name == "malformed":
            return dict(row, safe=[[True, None, "read"]] * 5)
        if fam.name == "hold_pdp":
            return dict(row, returned=[True, None, "erase"] if row["case"] in ("recorded_hold", "other_tenant") else [False, "x", "erase"])
        return None

    def shrunk(self):
        small = dict(DOC, params=dict(DOC["params"], malformed_per_seed=1000))
        return [("malformed_per_seed 1000 declared, 1000 malformed rows per seed", small,
                 [r for r in honest() if r["arm"] != "malformed" or r["i"] < 1000])]

    def test_short_probe_lists_fail(self):
        """The 5 probes per malformed document are part of the pinned design (PASSED at ea4677a with 2)."""
        rows = honest()
        k = next(j for j, r in enumerate(rows) if r["arm"] == "malformed")
        self.bad(rows[:k] + [dict(rows[k], safe=[DENY] * 2, lenient_ctl=[DENY] * 2)] + rows[k + 1:], "malformed row with 2 probes",
                 ("probes",))


if __name__ == "__main__":
    unittest.main()
