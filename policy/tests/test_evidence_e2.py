"""Evidence identity properties of verify/e2.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E2 and its
amendments, exp/e2_egress.py): the eight arms (enforced, six ctl_ arms, info_inject_only_disabled) x 5 seeds x scenarios i = 0..999
(identity (arm, seed, i)). A scenario's child rows (attempt, inject, payload, forge) are data dependent: the scenario row declares their
number in "items" and the children carry k = 0..items-1 (identity (arm, seed, i, k); category 2, parent-declared). "k" and "items" are
new in exp/e2_egress.py: the stamped rows lack them and the next stamped run must regenerate them. The synthetic children here are
minimal: 3 per enforced scenario, 1 per fifth control scenario."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS, PER = [71, 72, 73, 74, 75], 1000
ARMS = ["enforced", "ctl_model_port_disabled", "ctl_embed_disabled", "ctl_inbox_disabled", "ctl_export_disabled", "ctl_derive_join_dropped",
        "ctl_id_pattern_guard", "info_inject_only_disabled"]
CLASSES = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant", "plain"]
FORGES = ["extra_tenant", "extra_principal", "extra_now", "extra_recipient_object", "credential_unknown_key", "foreign_recipient", "unknown_recipient",
          "other_principals_authorization", "forged_session", "other_authority_session", "ended_session", "principal_as_session"]
OUTBOUND = ["embed", "export", "send_inbox", "send_model"]
DOC = {"name": "e2_egress", "params": {"seeds": SEEDS, "per_seed": PER, "classes": CLASSES, "forges": FORGES}, "result": {"verdict": "PASS"}}
BAD = {"kind": "model", "region": "us", "local": False, "family": "m3", "programme": "other"}
GOOD = {"kind": "model", "region": "eu", "local": True, "family": "m1", "programme": "cyber"}
CHANNELS = ["model", "embed", "inbox", "export"]


def _children(arm, s, i):
    base = {"arm": arm, "seed": s, "i": i}
    if arm == "enforced":
        return [dict(base, t="attempt", op="direct_send", classes=CLASSES[:5], attrs=BAD, outcome="denied:x", payloads=0),
                dict(base, t="payload", channel=CHANNELS[i % 4], attrs=GOOD, tenant="t1",
                     markers=[["@@%s@@" % c, c] for c in ("eu_only", "local_only", "model_banned", "restricted_channel", "plain")]),
                dict(base, t="forge", forge=FORGES[i % 12], path="inbox", classes=["plain"], outcome="denied:x", payloads=0)]
    out = []
    if arm.startswith("ctl_") and i % 5 == 0:
        out.append(dict(base, t="payload", channel="model", attrs=BAD, tenant="t1", markers=[["@@eu_only@@", "eu_only"]]))
    if arm == "ctl_id_pattern_guard" and s == SEEDS[0] and i == 0:
        out.append(dict(base, t="attempt", op="direct_send", classes=["plain"], attrs=GOOD, outcome="denied:id_pattern", payloads=0))
    return out


def honest(seeds=SEEDS, per=PER):
    rows = []
    for a in ARMS:
        for s in seeds:
            for i in range(per):
                ch = _children(a, s, i)
                rows += [dict(c, k=k) for k, c in enumerate(ch)]
                rows.append({"t": "scenario", "arm": a, "seed": s, "i": i, "payloads": 1, "registered": OUTBOUND,
                             "evaluated_actions": OUTBOUND + ["inject"], "items": len(ch)})
    return rows


class E2Evidence(Props, unittest.TestCase):
    V = load("e2")
    FAMILIES = (Fam("scenario", lambda r: r["t"] == "scenario", ["arm", "seed", "i"], index="i", seed="seed", arm="arm", end=lambda s, r: PER),
                Fam("child", lambda r: r["t"] != "scenario", ["arm", "seed", "i", "k"], index="k", seed="seed", arm="arm", end=lambda s, r: 3))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def sample(self, fam):
        rows = self.rows()
        if fam.name == "child":                 # an enforced payload child (k = 1), seed 72, scenario 500
            return next(j for j, r in enumerate(rows) if r["t"] == "payload" and r["arm"] == "enforced" and r["seed"] == 72 and r["i"] == 500), \
                next(j for j, r in enumerate(rows) if r["t"] == "forge" and r["arm"] == "enforced")
        return next(j for j, r in enumerate(rows) if r["t"] == "scenario" and r["arm"] == "ctl_inbox_disabled" and r["seed"] == 72 and r["i"] == 500), 0

    def break_row(self, fam, row):
        if fam.name == "child":
            return dict(row, attrs=BAD)          # a permitted payload now leaks
        return None

    def shrunk(self):
        return [("per_seed = 400 declared and run", dict(DOC, params=dict(DOC["params"], per_seed=400)), honest(per=400))]

    def test_child_rows_beyond_the_declared_count(self):
        """A child row past its scenario's declared count is an extra identity (a child copied into a scenario)."""
        rows = honest()
        j = next(x for x, r in enumerate(rows) if r["t"] == "forge" and r["arm"] == "enforced" and r["seed"] == 73)
        self.bad(rows + [dict(rows[j], k=3)], "a fourth child of a 3-item scenario", ("extra identity",))

    def test_scenario_with_its_children_dropped(self):
        """A control scenario dropped with its leaking child (PASSED at ea4677a: the control floor is a rate over present scenarios)."""
        rows = [r for r in honest() if not (r["arm"] == "ctl_embed_disabled" and r["seed"] == 74 and r["i"] == 10)]
        self.bad(rows, "ctl_embed_disabled 74/10 dropped with its child", ("missing identity",))


if __name__ == "__main__":
    unittest.main()
