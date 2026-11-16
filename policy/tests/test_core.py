import json
import unittest

from pol.core import apply, current_table, explain, genesis, manifest_hash

PR = {"u1": "human", "u2": "human", "a1": "agent"}
AU = {"r1": ["u1", "u2"], "r2": ["u1"], "r3": ["u1", "a1"]}


def run(events):
    st, codes = genesis(PR, AU, ["u1"]), []
    for e in events:
        st, c = apply(st, e)
        codes.append(c)
    return st, codes


def E(type, id, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id)}
    d.update(kw)
    return d


class T(unittest.TestCase):
    def test_remark_needs_typed_confirmation_on_other_channel(self):
        st, c = run([E("remark", "R", req="r2", principal="u1", value="X", channel="chat", based_on=0),
                     E("confirm", "K1", req="r2", principal="u1", pending="R", channel="chat", based_on=0),
                     E("confirm", "K2", req="r2", principal="u1", pending="R", channel="owner_ui", based_on=0)])
        self.assertEqual(c, ["pending_created", "channel_mismatch", "confirmed_applied"])
        self.assertEqual(st["reqs"]["r2"]["value"], "X")

    def test_agent_cannot_approve_own_proposal(self):
        st, c = run([E("propose", "P", req="r3", principal="a1", value="C", based_on=0),
                     E("decide", "D", req="r3", principal="a1", verb="accept", proposal="P", channel="agent_api", based_on=0)])
        self.assertEqual(c[-1], "self_approval")
        self.assertIsNone(st["reqs"]["r3"]["value"])

    def test_contested_then_resolve_and_explain(self):
        st, c = run([E("propose", "P1", req="r1", principal="a1", value="A", based_on=0),
                     E("propose", "P2", req="r1", principal="u2", value="B", based_on=0),
                     E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0),
                     E("decide", "D2", req="r1", principal="u2", verb="accept", proposal="P2", channel="owner_ui", based_on=0)])
        self.assertEqual(c[-1], "contested_opened")
        ex = explain(st, "r1")
        self.assertEqual(ex["status"], "contested")
        self.assertEqual(sorted(x["event"] for x in ex["contested"]["candidates"]), ["D1", "D2"])
        st, c = apply(st, E("decide", "R", req="r1", principal="u1", verb="resolve", choose="D2", channel="owner_ui", based_on=2))
        self.assertEqual(c, "resolved")
        st, c = apply(st, E("supersede", "S", req="r1", principal="u1", value="Z", channel="owner_ui", based_on=3))
        ex = explain(st, "r1")
        self.assertEqual((ex["value"], ex["decided_by"], ex["decision_event"]), ("Z", "u1", "S"))
        self.assertEqual([x["value"] for x in ex["superseded_chain"]], ["B"])

    def test_heads_are_tenant_wide_across_sessions(self):
        # a decision in session s1 binds and blocks a stale approval made from session s2 (state is per requirement, not per session)
        st, c = run([E("propose", "P1", req="r1", principal="u1", value="A", based_on=0, session="s1"),
                     E("propose", "P2", req="r1", principal="u2", value="B", based_on=0, session="s2"),
                     E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0, session="s1"),
                     E("supersede", "S1", req="r1", principal="u1", value="Z", channel="owner_ui", based_on=1, session="s1"),
                     E("decide", "D2", req="r1", principal="u2", verb="accept", proposal="P2", channel="owner_ui", based_on=0, session="s2")])
        self.assertEqual(c[-1], "stale")
        self.assertEqual(st["reqs"]["r1"]["value"], "Z")

    def test_agent_cannot_install_values(self):
        st, c = run([E("supersede", "S", req="r3", principal="a1", value="V", channel="agent_api", based_on=0),
                     E("propose", "P", req="r3", principal="u1", value="H", based_on=0),
                     E("decide", "A", req="r3", principal="a1", verb="amend", proposal="P", value="AV", channel="agent_api", based_on=0),
                     E("decide", "B", req="r3", principal="a1", verb="accept", proposal="P", channel="agent_api", based_on=0)])
        self.assertEqual(c, ["agent_not_permitted", "proposed", "agent_not_permitted", "decided_accept"])

    def test_stale_and_idempotent(self):
        base = [E("propose", "P1", req="r1", principal="u1", value="A", based_on=0),
                E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0)]
        st, c = run(base + [base[1], dict(base[1], value="x"),
                            E("supersede", "S", req="r1", principal="u1", value="Q", channel="owner_ui", based_on=0)])
        self.assertEqual(c[2:], ["duplicate", "idem_conflict", "stale"])
        self.assertEqual(st["reqs"]["r1"]["version"], 1)

    def test_revoke_invalidates_pending_and_blocks(self):
        st, c = run([E("remark", "R", req="r2", principal="u2", value="X", channel="chat", based_on=0),
                     E("revoke", "V", principal="u1", target="u2"),
                     E("propose", "P", req="r1", principal="u2", value="A", based_on=0)])
        self.assertEqual(c, ["pending_created", "revoked", "principal_revoked"])
        self.assertEqual(st["reqs"]["r2"]["pending"][0]["status"], "invalidated")

    def test_manifest_is_a_function_of_the_log(self):
        log = [E("propose", "P1", req="r1", principal="u1", value="A", based_on=0),
               E("decide", "D1", req="r1", principal="u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0)]
        a, _ = run(log)
        b, _ = run(log)
        self.assertEqual(manifest_hash(a, log), manifest_hash(b, log))
        self.assertEqual(json.loads(json.dumps(a)), a)
        self.assertEqual(current_table(a)[0]["value"], "A")


if __name__ == "__main__":
    unittest.main()
