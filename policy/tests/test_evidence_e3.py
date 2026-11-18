"""Evidence identity properties of verify/e3.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E3 and its
amendments, exp/e3_clash.py), per seed: PDP cases planted 2200, fault 2400 and context 3000 for the arms enforced and ctl_naive_pdp, legit
1400 for enforced (identity (arm, cat, seed, case)); 10 forge rounds of: session forgeries (11 kinds) on every declared surface entry for
enforced and on every non-pol.decisions entry for ctl_no_session_verify, request forgeries (15 kinds) on Outbox.enqueue for enforced and
ctl_naive_pdp, body forgeries (10 kinds) on DecisionService.submit for enforced and ctl_body_identity_trusted (identity (arm, cat, seed,
round, group, entry, forge)). "case" and "seed" are new in exp/e3_clash.py: the stamped rows lack them and the next stamped run must
regenerate them. Two seeds here so a seed relabel is exercised (THRESHOLDS stamps one)."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS = [399, 400]
SIZES = {"planted": 2200, "fault": 2400, "legit": 1400, "context": 3000}
ROUNDS = 10
ENTRIES = ["pol.decisions.DecisionService.erase_subject", "pol.decisions.DecisionService.explain", "pol.decisions.DecisionService.submit",
           "pol.decisions.DecisionService.table", "pol.directory.SessionAuthority.end", "pol.directory.SessionAuthority.verify",
           "pol.enforce.Enforcer.principal_of", "pol.enforce.GuardedChat.complete", "pol.enforce.GuardedEmbedder.embed",
           "pol.enforce.GuardedExport.export", "pol.enforce.Outbox.enqueue", "pol.enforce.Store.append", "pol.enforce.Store.append_derived",
           "pol.enforce.Store.context", "pol.enforce.Store.erase", "pol.enforce.Store.read"]
SESSION = ["handbuilt", "attacker_token_victim_id", "victim_token_copy", "principal_object", "none", "string", "dict", "ended_victim_session",
           "attacker_real_session", "other_tenant_session", "namespace_obj"]
REQUEST = ["tenant", "principal", "recipient_obj", "now", "region", "cred_tenant", "cred_clearance", "cred_groups", "cred_confusable", "cred_case",
           "cred_space", "cred_zero_width", "viewer", "trusted", "labels"]
BODY = ["body_tenant", "body_principal", "body_channel", "body_id", "body_value_ref", "body_namespaced_client_id", "body_traversal_client_id",
        "body_revoke_victim", "body_grant_self", "body_unknown_key"]
DOC = {"name": "e3_clash", "params": {"seeds": SEEDS, "planted": 2200, "fault": 2400, "legit": 1400, "context": 3000, "forge_rounds": ROUNDS,
                                       "surface_entries": [{"entry": e} for e in ENTRIES]}, "result": {"verdict": "PASS"}}
REFUSED = {"type": "refused", "reason": "x"}
# an accepted observation for every entry kind the verifier knows (the controls must reach their floors)
ACCEPTED = {"type": "value", "text": "t", "id": 1, "ok": [1], "value": "queued", "payloads": [1], "principal": "alice", "victim_session_still_valid": False,
            "code": "proposed"}


def _pdp(arm, cat, seed, case):
    allow = cat == "legit" or arm != "enforced"
    return {"arm": arm, "cat": cat, "sent": {}, "returned": {"allow": allow, "reason": None if allow else "forged_request"}, "case": case, "seed": seed}


def _forge(arm, seed, rnd, group, entry, fk):
    ret = REFUSED if arm == "enforced" else dict(ACCEPTED, value=True if entry.endswith("SessionAuthority.verify") else ACCEPTED["value"])
    if group == "body":
        ret = {"type": "value", "code": "refused_x" if arm == "enforced" else "proposed"}
    return {"arm": arm, "cat": "forge", "round": rnd, "group": group, "entry": entry, "forge": fk, "sent": {}, "session_principal_id": "alice",
            "victim": "alice" if group == "session" else None, "returned": ret, "seed": seed}


def honest(seeds=SEEDS, sizes=SIZES, rounds=ROUNDS, entries=ENTRIES):
    rows = []
    for s in seeds:
        for cat, n in sizes.items():
            for i in range(n):
                rows += [_pdp(a, cat, s, i) for a in (("enforced",) if cat == "legit" else ("enforced", "ctl_naive_pdp"))]
        for rnd in range(rounds):
            for e in entries:
                rows += [_forge("enforced", s, rnd, "session", e, fk) for fk in SESSION]
                if not e.startswith("pol.decisions"):
                    rows += [_forge("ctl_no_session_verify", s, rnd, "session", e, fk) for fk in SESSION]
            rows += [_forge(a, s, rnd, "request", "pol.enforce.Outbox.enqueue", fk) for a in ("enforced", "ctl_naive_pdp") for fk in REQUEST]
            rows += [_forge(a, s, rnd, "body", "pol.decisions.DecisionService.submit", fk) for a in ("enforced", "ctl_body_identity_trusted") for fk in BODY]
    return rows


class E3Evidence(Props, unittest.TestCase):
    V = load("e3")
    FAMILIES = (Fam("pdp", lambda r: r["cat"] != "forge", ["arm", "cat", "seed", "case"], index="case", seed="seed", arm="arm",
                    end=lambda s, r: SIZES[r["cat"]]),
                Fam("forge", lambda r: r["cat"] == "forge", ["arm", "cat", "seed", "round", "group", "entry", "forge"], index="round", seed="seed",
                    arm="arm", end=lambda s, r: ROUNDS))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def sample(self, fam):
        rows = self.rows()
        if fam.name == "pdp":
            return next(k for k, r in enumerate(rows) if r["cat"] == "planted" and r["arm"] == "enforced" and r["case"] == 1000), 0
        return next(k for k, r in enumerate(rows) if r["cat"] == "forge" and r["arm"] == "enforced" and r["round"] == 5 and r["group"] == "session"
                    and r["entry"] == "pol.enforce.Store.read"), next(k for k, r in enumerate(rows) if r["cat"] == "forge")

    def break_row(self, fam, row):
        if fam.name == "pdp":
            return dict(row, returned={"allow": True, "reason": None})
        return dict(row, returned=dict(ACCEPTED))

    def shrunk(self):
        small = dict(SIZES, planted=1100)
        return [("planted = 1100 declared and run", dict(DOC, params=dict(DOC["params"], planted=1100)), honest(sizes=small)),
                ("forge_rounds = 2 declared and run", dict(DOC, params=dict(DOC["params"], forge_rounds=2)), honest(rounds=2)),
                ("surface_entries cut to four, rows to match", dict(DOC, params=dict(DOC["params"], surface_entries=[{"entry": e} for e in ENTRIES[-4:]])),
                 honest(entries=ENTRIES[-4:]))]


if __name__ == "__main__":
    unittest.main()
