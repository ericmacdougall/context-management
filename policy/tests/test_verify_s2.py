"""verify/s2.py re-judges the S2 rows (step 2) without trusting the experiment: a synthetic row set built to the rules passes, and each
single break fails (an accepted attack, a vacuous attack, a valid proof that does not authenticate, a concurrent replay with two
winners, a bad token that reads data, another tenant's token that sees the seeded proposal, a missing token pair, a wrong origin, a
body origin accepted, a look-alike install accepted, a control that does not catch). Stdlib only."""
import copy
import importlib.util
import os
import unittest

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("verify_s2_under_test", os.path.join(POLICY, "verify", "s2.py"))
S2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S2)

CLASSES = ["valid", "forged_mac", "body_edited", "other_key", "other_factor_id", "look_alike", "other_principal", "other_channel",
           "other_tenant", "expired", "far_future", "replay", "revoked_factor", "malformed"]
TOKENS = ["valid", "forged", "expired", "revoked", "ended", "empty", "other_tenant"]
CALLS = ["submit", "explain", "read_table", "erase_candidates", "erase_values"]
CONTROLS = ["ctl_bare_mint_80bb6a7", "ctl_bare_mint_0a20b64", "ctl_no_replay", "ctl_no_mac", "ctl_identity_from_body", "ctl_origin_from_body"]
DOC = {"params": {"seeds": [1], "trials_per_class": 2, "classes": CLASSES, "tokens": TOKENS, "calls": CALLS, "controls": CONTROLS}}


def good_rows(seed=1):
    R = []
    add = lambda arm, **kw: R.append(dict(arm=arm, seed=seed, **kw))                     # noqa: E731
    for i in range(S2.MINT_EXPECTED):
        add("real", kind="mint", role="r", what="w%d" % i, outcome="refused")
    for cls in CLASSES + ["revoked_principal"]:
        for _ in range(2):
            if cls == "valid":
                add("real", kind="proof", cls=cls, outcome="accepted", factor_principal="alice", claimed="alice", stamped="alice", authenticated=True)
            else:
                claimed = "bob" if cls in S2.CLAIMS_OTHER else "alice"
                add("real", kind="proof", cls=cls, outcome="refused", factor_principal="alice", claimed=claimed, stamped=None)
            if cls == "replay":
                add("real", kind="proof", cls="replay_first", outcome="accepted", principal="alice", stamped="alice", authenticated=True)
    add("real", kind="replay_concurrent", round=0, presented=8, winners=1)
    for c in TOKENS:
        if c == "valid":
            obs = {"submit": "unauthorized", "explain": {"tenant": "tenant-a", "seen": 1}, "read_table": {"n": 2, "seen": 1},
                   "erase_candidates": {"refused": None, "values": 1}, "erase_values": {"refused": None, "erased": 0}}
        elif c == "other_tenant":
            obs = {"submit": "unauthorized", "explain": {"tenant": "tenant-b", "seen": 0}, "read_table": {"n": 2, "seen": 0},
                   "erase_candidates": {"refused": None, "values": 0}, "erase_values": {"refused": None, "erased": 0}}
        else:
            obs = {"submit": "unauthenticated:invalid_session", "explain": None, "read_table": {"n": 0, "seen": 0},
                   "erase_candidates": {"refused": "unauthorized", "values": 0}, "erase_values": {"refused": "unauthorized", "erased": None}}
        for k in CALLS:
            add("real", kind="token", cls=c, call=k, obs=obs[k])
    add("real", kind="token_after", ended_returned=True, dana_values=1)
    for pid, kind, ch, a, t in (("alice", "human", "owner_ui", "user", "user_typed"), ("bot", "agent", "agent_api", "assistant", "agent_generated")):
        add("real", kind="origin", principal=pid, principal_kind=kind, channel=ch, code="proposed", actor=a, trust_origin=t)
    for k in S2.BODY_KEYS:
        add("real", kind="body_identity", key=k, code="identity_in_body", event_actor=None)
    add("real", kind="install_lookalike", base="alice", look_alike="a1ice", outcome="refused:ValueError")
    add("ctl_bare_mint_80bb6a7", kind="mint", outcome="accepted")
    add("ctl_bare_mint_0a20b64", kind="mint", outcome="accepted", authenticated=True)
    add("ctl_no_replay", kind="proof", cls="replay", outcome="accepted")
    add("ctl_no_mac", kind="proof", cls="forged_mac", outcome="accepted")
    add("ctl_identity_from_body", kind="proof", cls="other_principal", outcome="accepted", factor_principal="alice", stamped="bob")
    add("ctl_origin_from_body", kind="body_identity", key="actor", code="proposed", event_actor="user")
    return R


def _find(rows, **kw):
    return next(r for r in rows if all(r.get(k) == v for k, v in kw.items()))


class VerifyS2(unittest.TestCase):
    def _v(self, rows):
        return S2.verify(DOC, rows)["verdict"]

    def test_good_set_passes(self):
        self.assertEqual(self._v(good_rows()), "PASS")

    def _broken(self, change, **where):
        rows = copy.deepcopy(good_rows())
        change(_find(rows, **where))
        return self._v(rows)

    def test_each_break_fails(self):
        breaks = {
            "accepted attack": (lambda r: r.update(outcome="accepted", stamped="alice"), dict(arm="real", cls="other_key")),
            "vacuous attack": (lambda r: r.update(claimed="alice"), dict(arm="real", cls="look_alike")),
            "valid proof not authenticated": (lambda r: r.update(authenticated=False), dict(arm="real", cls="valid")),
            "valid proof stamped as another": (lambda r: r.update(stamped="bob"), dict(arm="real", cls="valid")),
            "accepted mint": (lambda r: r.update(outcome="accepted"), dict(arm="real", kind="mint")),
            "two concurrent winners": (lambda r: r.update(winners=2), dict(arm="real", kind="replay_concurrent")),
            "ended token reads": (lambda r: r.update(obs={"n": 2, "seen": 1}), dict(arm="real", kind="token", cls="ended", call="read_table")),
            "forged token submits": (lambda r: r.update(obs="unauthorized"), dict(arm="real", kind="token", cls="forged", call="submit")),
            "other tenant sees": (lambda r: r.update(obs={"tenant": "tenant-b", "seen": 1}), dict(arm="real", kind="token", cls="other_tenant", call="explain")),
            "other tenant erases": (lambda r: r.update(obs={"refused": None, "erased": 1}), dict(arm="real", kind="token", cls="other_tenant", call="erase_values")),
            "valid token positive control": (lambda r: r.update(obs=None), dict(arm="real", kind="token", cls="valid", call="explain")),
            "value lost": (lambda r: r.update(dana_values=0), dict(arm="real", kind="token_after")),
            "agent stamped user": (lambda r: r.update(actor="user", trust_origin="user_typed"), dict(arm="real", kind="origin", principal="bot")),
            "body actor accepted": (lambda r: r.update(code="proposed", event_actor="user"), dict(arm="real", kind="body_identity", key="actor")),
            "look-alike installed": (lambda r: r.update(outcome="accepted"), dict(arm="real", kind="install_lookalike")),
            "control 80bb6a7 silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_bare_mint_80bb6a7")),
            "control 0a20b64 not authenticated": (lambda r: r.update(authenticated=False), dict(arm="ctl_bare_mint_0a20b64")),
            "control replay silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_no_replay")),
            "control mac silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_no_mac")),
            "control identity stamps the factor's own": (lambda r: r.update(stamped="alice"), dict(arm="ctl_identity_from_body")),
            "control origin silent": (lambda r: r.update(event_actor="assistant"), dict(arm="ctl_origin_from_body")),
        }
        for name, (change, where) in breaks.items():
            self.assertEqual(self._broken(change, **where), "FAIL", name)

    def test_missing_rows_fail(self):
        for name, drop in (("token pair", lambda r: r["kind"] == "token" and r["cls"] == "revoked" and r["call"] == "erase_values"),
                           ("proof class", lambda r: r.get("cls") == "expired"),
                           ("concurrency", lambda r: r["kind"] == "replay_concurrent"),
                           ("a control arm", lambda r: r["arm"] == "ctl_no_mac"),
                           ("a body key", lambda r: r["kind"] == "body_identity" and r["arm"] == "real" and r["key"] == "trust_origin")):
            self.assertEqual(self._v([r for r in good_rows() if not drop(r)]), "FAIL", name)
        self.assertEqual(S2.verify(dict(DOC, params=dict(DOC["params"], seeds=[1, 2])), good_rows())["verdict"], "FAIL", "seed 2 missing")


if __name__ == "__main__":
    unittest.main()
