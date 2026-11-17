"""Evidence identity properties of verify/e4.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E4, amendments
2-3, exp/e4_tighten.py): arms fenced, positive, control_enqueue_only; the 15 (path, tightening) combinations; R = 200 runs i = 0..199 per
arm and combination, run i on seed seeds[i % len(seeds)] (5 seeds). Identity: (arm, combo, seed, i)."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS = [71, 72, 73, 74, 75]
R = 200
COMBOS = [("model", "model_banned"), ("model", "region_removed"), ("model", "sender_revoked"), ("model", "tenant_killed"),
          ("inbox", "model_banned"), ("inbox", "region_removed"), ("inbox", "recipient_revoked"), ("inbox", "tenant_killed"), ("inbox", "group_removed"),
          ("export", "region_removed"), ("export", "sender_revoked"), ("export", "tenant_killed"),
          ("inbox", "label_narrowed"), ("inbox", "source_erased"), ("inbox", "legal_hold_recorded")]
CHANGES = {"model_banned", "region_removed", "sender_revoked", "recipient_revoked", "tenant_killed"}
DOC = {"name": "e4_tighten", "params": {"R": R, "combos": [list(c) for c in COMBOS], "seeds": SEEDS}, "result": {"verdict": "PASS"}}


def _run(arm, path, t, i):
    r = {"arm": arm, "combo": "%s/%s" % (path, t), "seed": SEEDS[i % len(SEEDS)], "i": i, "enqueue": {"allow": True, "policy_version": 1}}
    if arm == "fenced":
        if t == "source_erased":
            r.update(refused=["source_erased"], audit_after=[{"action": "erase", "allow": True, "reason": None, "policy_version": 1}])
        else:
            r.update(refused=["denied"], audit_after=[{"action": "send", "allow": False, "reason": "x", "policy_version": 2 if t in CHANGES else 1}])
        r.update(delivered=0, sent=[])
    else:
        r.update(refused=[], audit_after=[], delivered=1, sent=[{"text_has_marker": True, "rcp_obj": 7, "checked_rcp_obj": 7}])
    return r


def honest():
    return [_run(a, p, t, i) for a in ("fenced", "positive", "control_enqueue_only") for p, t in COMBOS for i in range(R)]


class E4Evidence(Props, unittest.TestCase):
    V = load("e4")
    FAMILIES = (Fam("run", lambda r: True, ["arm", "combo", "seed", "i"], index="i", seed="seed", arm="arm", end=lambda s, r: R),)

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def break_row(self, fam, row):
        return dict(row, delivered=1, sent=[{"text_has_marker": True, "rcp_obj": 7, "checked_rcp_obj": 7}]) if row["arm"] == "fenced" else None

    def sample(self, fam):
        rows = self.rows()
        i = next(k for k, r in enumerate(rows) if r["arm"] == "fenced" and r["i"] == 100)          # a fenced run (break_row needs one)
        return i, 0

    def shrunk(self):
        half = dict(DOC, params=dict(DOC["params"], R=100))
        four = dict(DOC, params=dict(DOC["params"], seeds=SEEDS[:4]))
        return [("R = 100 declared and run", half, [r for r in honest() if r["i"] < 100]),
                ("four seeds declared, runs rotated over them", four, [dict(r, seed=SEEDS[r["i"] % 4]) for r in honest()])]

    def test_audit_probe_one_run_copied(self):
        """The audit's probe 2: the positive arm is one run (model/model_banned) copied 3000x (PASSED at ea4677a). Copies given
        distinct identities are NOT detectable by identity alone (no nonce in E4): a limit stated in the amendment text, not tested."""
        rows = honest()
        one = next(r for r in rows if r["arm"] == "positive")
        out = [r if r["arm"] != "positive" else dict(one) for r in rows]
        self.bad(out, "3000 byte-identical copies of one positive run", ("duplicate identity", "missing identity"))


if __name__ == "__main__":
    unittest.main()
