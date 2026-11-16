"""Reproducers for the independent review of 515e594 (findings H1-H9, M1-M4). Written to run on both the old and the fixed API:
each test states the property that must hold; at 515e594 they FAIL, after the fix round they pass."""
import unittest

from exp.gen import base_policy, GRANTS
from pol.core import apply, genesis
from pol.enforce import Block, Enforcer, Outbox, PolicyDenied, Record, Store
from pol.labels import Label, LabelError
from pol.pdp import Dest, Policy, Principal, decide

PR = {"u1": "human", "u2": "human", "a1": "agent", "n1": "analyst"}
AU = {"r1": ["u1", "u2", "a1", "n1"], "r2": ["u1"]}
LAB = {"tenants": ["T1"], "acl": [], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False, "restricted": False,
       "holds": [], "subjects": []}


def mk():
    try:
        return genesis("T1", PR, AU, ["u1"])
    except TypeError:
        return genesis(PR, AU, ["u1"])


def E(type, id, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id), "tenant": "T1"}
    if type in ("propose", "remark", "supersede") or (type == "decide" and kw.get("verb") == "amend"):
        d["label"] = dict(LAB)
    d.update(kw)
    return d


def run(events, st=None):
    st = st or mk()
    codes = []
    for e in events:
        st, c = apply(st, e)
        codes.append(c)
    return st, codes


def ph(pid, value, author):
    from pol.core import sha
    return sha([pid, value, author])


class Review(unittest.TestCase):
    def test_h1_region_from_principal_not_context(self):
        pol = base_policy()
        us = Principal("p", "t1", frozenset(), 3, True, region="US")
        r = decide(us, "read", Label(["t1"], regions=["EU"]), {"region": "EU"}, policy=pol)
        self.assertFalse(r.allow)

    def test_h1_expiry_uses_trusted_clock(self):
        pol = base_policy()
        p = Principal("P1", "t1", frozenset(), 3, True, region="EU")
        ctx = {"destination": "d_model_cyber", "authorization": "auth1", "now": 1}
        r = decide(p, "send_model", Label(["t1"], restricted=True), ctx, policy=pol, **({"trusted": {"now": 5000}} if _has_trusted() else {}))
        self.assertFalse(r.allow)

    def test_h1_outbox_refuses_forged_recipient(self):
        pol = base_policy()
        sender = Principal("s", "t1", frozenset(["fin"]), 3, True, region="EU")
        carol = Principal("carol", "t1", frozenset(), 0, False, region="US", model="d_model_us")
        enf = Enforcer(lambda: pol)
        got = []
        ob = Outbox(enf, {"s": sender, "carol": carol}.get, "send_inbox", lambda p, b, c: got.append(b))
        lab = Label(["t1"], acl=[["fin"]], sensitivity=3, regions=["EU"], local_only=True)
        try:
            forged = Principal("carol", "t1", frozenset(["fin"]), 3, True, region="EU", model=None)
            ob.enqueue("s", [Block("SECRET", lab)], {"recipient": forged}, None)
            ob.deliver_all()
        except PolicyDenied:
            pass
        self.assertEqual(got, [])

    def test_h2_misspelled_narrowing_key_denies(self):
        pol = base_policy()
        p = Principal("p", "t1", frozenset(), 3, True, region="EU")
        for k in ("CAP_ACTIONS", "cap-actions", "Cap_Actions", "cap_actions​", "cap_clearance "):
            val = ["read"] if "actions" in k.lower() else 0
            r = decide(p, "send_model", Label(["t1"]), {"destination": "d_model_us", "credential": {k: val}}, policy=pol)
            self.assertFalse(r.allow, k)

    def test_h3_unknown_kind_gets_agent_restrictions(self):
        st, c = run([E("propose", "P", req="r1", principal="a1", value="X", based_on=0),
                     E("decide", "D", req="r1", principal="n1", verb="accept", proposal="P", channel="anything", based_on=0, proposal_hash=ph("P", "X", "a1"))])
        self.assertNotEqual(c[-1], "decided_accept")

    def test_h4_agent_cannot_reopen_contested(self):
        st, c = run([E("propose", "P1", req="r1", principal="u1", value="A", based_on=0),
                     E("propose", "P2", req="r1", principal="u2", value="B", based_on=0),
                     E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0, proposal_hash=ph("P1", "A", "u1")),
                     E("decide", "D2", req="r1", principal="u2", verb="accept", proposal="P2", channel="owner_cli", based_on=0, proposal_hash=ph("P2", "B", "u2")),
                     E("decide", "R", req="r1", principal="u1", verb="resolve", choose="D1", channel="owner_ui", based_on=2),
                     E("propose", "J", req="r1", principal="a1", value="junk", based_on=3),
                     E("decide", "AJ", req="r1", principal="a1", verb="reject", proposal="J", channel="agent_api", based_on=2, proposal_hash=ph("J", "junk", "a1"))])
        self.assertEqual(st["reqs"]["r1"]["status"], "current")

    def test_h5_rejected_proposal_never_installed(self):
        st, c = run([E("propose", "BAD", req="r1", principal="u2", value="BAD", based_on=0), E("propose", "OK", req="r1", principal="u2", value="OK", based_on=0),
                     E("decide", "D0", req="r1", principal="u1", verb="reject", proposal="BAD", channel="owner_ui", based_on=0, proposal_hash=ph("BAD", "BAD", "u2")),
                     E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="OK", channel="owner_ui", based_on=1, proposal_hash=ph("OK", "OK", "u2")),
                     E("decide", "D2", req="r1", principal="u2", verb="accept", proposal="BAD", channel="owner_cli", based_on=1, proposal_hash=ph("BAD", "BAD", "u2")),
                     E("decide", "R", req="r1", principal="u1", verb="resolve", choose="D2", channel="owner_ui", based_on=3)])
        self.assertNotEqual(st["reqs"]["r1"]["value"], "BAD")

    def test_h6_duplicate_ids_refused(self):
        st, c = run([E("propose", "p1", req="r1", principal="a1", value="ATTACKER", based_on=0), E("propose", "p1", req="r1", principal="u2", value="HONEST", based_on=0, idem="x2")])
        self.assertEqual(c[1], "duplicate_id")

    def test_h7_explain_needs_a_viewer(self):
        from pol import view
        st, _ = run([E("propose", "P", req="r2", principal="u1", value="X", based_on=0)])
        out = view.explain_for(st, "r2", Principal("v", "t2", frozenset(), 3, True, region="EU"), base_policy())
        self.assertNotIn("X", str(out))

    def test_h7_unlabelled_install_refused(self):
        st, c = run([E("supersede", "S", req="r1", principal="u1", value="V", channel="owner_ui", based_on=0, label=None)])
        self.assertNotEqual(c[0], "superseded")

    def test_h8_label_narrowed_after_enqueue(self):
        pol = base_policy()
        s = Principal("s", "t1", frozenset(["g1"]), 3, True, region="EU")
        bob = Principal("bob", "t1", frozenset(["g1"]), 3, False, region="EU")
        enf = Enforcer(lambda: pol)
        store = Store(enf)
        store.records["rec"] = Record("rec", "LAYOFF", Label(["t1"], acl=[["g1"]]))
        got = []
        ob = Outbox(enf, {"s": s, "bob": bob}.get, "send_inbox", lambda p, b, c: got.append(b), resolver=store.records.get) if _outbox_has_resolver() else Outbox(
            enf, {"s": s, "bob": bob}.get, "send_inbox", lambda p, b, c: got.append(b))
        r = store.records["rec"]
        kw = {"record_ids": ["rec"]} if _outbox_has_resolver() else {}
        ob.enqueue("s", [Block(r.text, r.label)], {}, "bob", **kw)
        store.records["rec"] = Record("rec", "LAYOFF", Label(["t1"], acl=[["hr"]]))
        ob.deliver_all()
        self.assertEqual(got, [])

    def test_h9_label_roundtrip_is_lossless_and_strict(self):
        d = Label(["t1"], holds=["h1"], subjects=["s1"]).to_dict()
        self.assertEqual(Label.from_dict(d), Label(["t1"], holds=["h1"], subjects=["s1"]))
        bad = dict(d)
        bad.pop("holds")
        with self.assertRaises(LabelError):
            Label.from_dict(bad)
        with self.assertRaises(LabelError):
            Label.from_dict(dict(d, extra=1))

    def test_m1_events_for_another_tenant_refused(self):
        st = mk()
        st, c = apply(st, dict(E("revoke", "V", principal="u1", target="u2"), tenant="T2"))
        self.assertNotEqual(c, "revoked")

    def test_m2_retry_returns_original_outcome(self):
        from pol import core
        st, _ = run([E("propose", "P", req="r1", principal="u1", value="A", based_on=0)])
        ev = E("decide", "D", req="r1", principal="u1", verb="accept", proposal="P", channel="owner_ui", based_on=0, proposal_hash=ph("P", "A", "u1"))
        st, c = apply(st, ev)
        self.assertEqual(c, "decided_accept")
        _s, c2, orig = core.apply_ex(st, ev)
        self.assertEqual((c2, orig), ("duplicate", "decided_accept"))

    def test_m2_refused_request_does_not_burn_key(self):
        st = mk()
        ev = E("propose", "P", req="r2", principal="u2", value="A", based_on=0, idem="k")
        st, c1 = apply(st, dict(ev, principal="zz"))
        st, c2 = apply(st, ev)
        self.assertEqual(c2, "proposed")

    def test_m3_model_ban_and_revocation_canonical(self):
        pol = Policy(1, grants=GRANTS, banned_models=frozenset(["gpt-x"]), revoked=frozenset(["Mallory"]),
                     destinations={"d": Dest("model", "EU", False, 3, model="GPT-X-2025-01")})
        p = Principal("p", "t1", frozenset(), 3, True, region="EU")
        self.assertFalse(decide(p, "send_model", Label(["t1"]), {"destination": "d"}, policy=pol).allow)
        m = Principal("ｍallory", "t1", frozenset(), 3, True, region="EU")
        self.assertFalse(decide(m, "read", Label(["t1"]), {}, policy=pol).allow)

    def test_m4_default_notice_none_and_no_cross_tenant(self):
        from pol.threads import World, Item, build_view
        a = Item("a", "x", "ta", "v", "d", 1, Label(["t1"]))
        b = Item("b", "y", "tb", "v", "d", 1, Label(["t2"]))
        w = World({"a": a, "b": b}, [("a", "b", "conflicts_with")], [], {})
        v = Principal("v", "t1", frozenset(), 3, True, region="EU")
        view = build_view(w, v, Policy(1, grants=GRANTS), None)
        self.assertEqual(view["notices"], [])
        view = build_view(w, v, Policy(1, grants=GRANTS, conflict_notice="existence_only"), None)
        self.assertEqual(view["notices"], [])


def _has_trusted():
    import inspect
    return "trusted" in inspect.signature(decide).parameters


def _outbox_has_resolver():
    import inspect
    return "resolver" in inspect.signature(Outbox.__init__).parameters


if __name__ == "__main__":
    unittest.main()
