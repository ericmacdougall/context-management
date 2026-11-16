"""Red reproducers pinned to the 77ea895 API (feat/policy-decisions). Each test asserts the property the rescue must establish and
FAILS at 77ea895 with the stated reason (an assertion, never an import or attribute error). They are historical: the rebuilt API
removes the symbols they call, so at the rescue HEAD they are not run; the same properties are asserted against the new API in
tests/test_findings.py. Run: python -m unittest tests.red.test_red_77ea895 (from policy/, at 77ea895 plus this file)."""
import threading
import time
import unittest

from pol.core import apply, genesis
from pol.enforce import Block, Enforcer, Outbox, PolicyDenied, Record, Store
from pol.labels import Label
from pol.pdp import ACTIONS, Dest, Policy, PolicyError, Principal, decide, parse_policy
from pol.testkit import LAB, prepare

GR = {"t1": frozenset(ACTIONS), "T1": frozenset(ACTIONS)}
POL = Policy(1, grants=GR, destinations={"cy": Dest("model", "US", False, 3, model="m4", programme="cyber"),
                                         "m_us": Dest("model", "US", False, 3, model="m9")},
             authorizations={"a_old": {"tenant": "t1", "programme": "cyber", "expires": 1000, "principal": "bob"}})
BOB = Principal("bob", "t1", frozenset({"g1"}), 3, True, region="US")
EU = Label(tenants=["t1"], regions=["EU"])


def allowed(fn):
    try:
        d = fn()
    except PolicyDenied:
        return False
    return bool(getattr(d, "allow", True))


def run(st, ev):
    return apply(st, prepare(st, ev))


class N1(unittest.TestCase):
    def test_enforcer_without_directory_must_not_trust_caller_principal(self):
        forged = Principal("bob", "t1", frozenset({"g1"}), 3, True, region="EU")
        self.assertFalse(allowed(lambda: Enforcer(lambda: POL).check(forged, "read", [EU], {})),
                         "N1: Enforcer() with no directory used the caller-built Principal (region EU) and allowed an EU-only read")

    def test_store_read_through_default_enforcer_must_not_return_eu_data(self):
        st = Store(Enforcer(lambda: POL))
        st.records["r"] = Record("r", "EU-SECRET", EU)
        forged = Principal("bob", "t1", frozenset({"g1"}), 3, True, region="EU")
        got = None
        try:
            got = st.read(forged, "r", {}).text
        except PolicyDenied:
            pass
        self.assertIsNone(got, "N1: Store.read returned %r to a forged Principal through the default Enforcer" % got)

    def test_caller_chosen_clock_must_not_revive_an_expired_authorisation(self):
        r = Label(tenants=["t1"], restricted=True)
        a = allowed(lambda: decide(BOB, "send_model", r, {"destination": "cy", "authorization": "a_old"}, policy=POL, trusted={"now": 5}))
        b = allowed(lambda: Enforcer(lambda: POL, directory={"bob": BOB}.get, clock=lambda: 5).check(BOB, "send_model", [r], {"destination": "cy", "authorization": "a_old"}))
        self.assertFalse(a or b, "N1: an authorisation that expired at 1000 was accepted under a caller-chosen clock (decide trusted=%s, Enforcer clock=%s)" % (a, b))

    def test_raising_directory_must_deny(self):
        def broken(_pid):
            raise RuntimeError("directory down")
        ok = None
        try:
            ok = Enforcer(lambda: POL, directory=broken).check(BOB, "read", [Label(tenants=["t1"])], {}).allow
        except PolicyDenied:
            ok = False
        except RuntimeError:
            ok = "raised"
        self.assertIs(ok, False, "N1: a failing directory did not produce a logged deny (got %r)" % ok)


class H1Residue(unittest.TestCase):
    def test_viewer_scoped_explain_must_not_trust_caller_viewer_object(self):
        from pol.view import explain_for
        st = genesis("T1", {"u1": "human"}, {"r": ["u1"]}, ["u1"])
        hr = dict(LAB, acl=[["hr"]])
        st, _ = run(st, dict(type="propose", id="p", idem="p", req="r", principal="u1", value="salary=250k", based_on=0, label=hr))
        st, _ = run(st, dict(type="decide", id="a", idem="a", req="r", principal="u1", verb="accept", proposal="p", channel="owner_ui", based_on=0))
        forged_viewer = Principal("ivan", "T1", frozenset({"hr"}), 3)          # ivan is not in hr; the object says he is
        out = explain_for(st, "r", forged_viewer, Policy(1, grants=GR))
        self.assertNotEqual(out.get("value"), "salary=250k", "H1: explain_for took the viewer object from the caller and showed the hr value")


class N2(unittest.TestCase):
    def test_principal_without_role_cannot_propose(self):
        st = genesis("T1", {"u1": "human", "x": "agent"}, {"r": ["u1"]}, ["u1"])
        st, code = run(st, dict(type="propose", id="q", idem="q", req="r", principal="x", value="v", based_on=0))
        self.assertNotEqual(code, "proposed", "N2: a principal with no authority or proposer role stored a proposal")

    def test_open_proposals_are_capped_per_principal(self):
        st = genesis("T1", {"u1": "human"}, {"r": ["u1"]}, ["u1"])
        codes = []
        for i in range(200):
            st, c = run(st, dict(type="propose", id="q%d" % i, idem="q%d" % i, req="r", principal="u1", value="v", based_on=0))
            codes.append(c)
        self.assertLess(codes.count("proposed"), 200, "N2: 200 open proposals from one principal on one requirement were all stored (no cap)")

    def test_cost_per_event_does_not_grow_with_history(self):
        st = genesis("T1", {"u1": "human"}, {"r": ["u1"]}, ["u1"])
        blk = []
        n, step = 1600, 200
        for b in range(n // step):
            t0 = time.perf_counter()
            for i in range(b * step, (b + 1) * step):
                st, _ = run(st, dict(type="propose", id="q%d" % i, idem="q%d" % i, req="r", principal="u1", value="v" * 50, based_on=0))
                st, _ = run(st, dict(type="decide", id="x%d" % i, idem="x%d" % i, req="r", principal="u1", verb="reject", proposal="q%d" % i,
                                     channel="owner_ui", based_on=st["reqs"]["r"]["version"]))
            blk.append(time.perf_counter() - t0)
        ratio = blk[-1] / blk[0]
        self.assertLess(ratio, 3.0, "N2: per-event cost grows with history: last block %.3fs vs first %.3fs (x%.1f)" % (blk[-1], blk[0], ratio))


class N3(unittest.TestCase):
    def test_agent_churn_cannot_starve_a_human_decision(self):
        st = genesis("T1", {"u1": "human", "a1": "agent"}, {"r": ["u1", "a1"]}, ["u1"])
        st, _ = run(st, dict(type="propose", id="pA", idem="pA", req="r", principal="u1", value="A", based_on=0))
        ok = False
        for attempt in range(5):
            seen = st["reqs"]["r"]["version"]                                 # the human reads the head
            st, _ = run(st, dict(type="propose", id="j%d" % attempt, idem="j%d" % attempt, req="r", principal="a1", value="junk", based_on=seen))
            st, _ = run(st, dict(type="decide", id="x%d" % attempt, idem="x%d" % attempt, req="r", principal="a1", verb="reject",
                                 proposal="j%d" % attempt, channel="agent_api", based_on=seen))
            st, c = run(st, dict(type="decide", id="h%d" % attempt, idem="h%d" % attempt, req="r", principal="u1", verb="accept", proposal="pA",
                                 channel="owner_ui", based_on=seen))
            if c == "decided_accept":
                ok = True
                break
        self.assertTrue(ok, "N3: an agent proposing and rejecting its own junk moved the head before each of 5 human attempts; every one was stale")


class H2(unittest.TestCase):
    def test_non_canonical_narrowing_keys_deny(self):
        leaks = []
        for k in ("cаp_actions", "caṕ_actions", "scope", "allowed_actions", "caps_actions"):
            if allowed(lambda: decide(BOB, "send_model", Label(tenants=["t1"]), {"destination": "m_us", "credential": {k: ["read"]}}, policy=POL, trusted={"now": 1})):
                leaks.append(k)
        self.assertEqual(leaks, [], "H2: credential scoped to read, key spelt %r, still allowed send_model" % leaks)


class H6(unittest.TestCase):
    def test_agent_cannot_squat_the_id_a_human_will_use(self):
        st = genesis("T1", {"u1": "human", "a1": "agent"}, {"r": ["u1", "a1"]}, ["u1"])
        st, _ = run(st, dict(type="propose", id="p", idem="p", req="r", principal="u1", value="V", based_on=0))
        st, _ = run(st, dict(type="propose", id="dec-1", idem="sq", req="r", principal="a1", value="x", based_on=0))
        st, c = run(st, dict(type="decide", id="dec-1", idem="d1", req="r", principal="u1", verb="accept", proposal="p", channel="owner_ui", based_on=0))
        self.assertEqual(c, "decided_accept", "H6: the agent pre-registered id 'dec-1'; the human's decision was refused %s" % c)


class H7(unittest.TestCase):
    def test_unscoped_reads_not_exported(self):
        import pol.core as core
        exported = [n for n in ("explain", "current_table") if hasattr(core, n) and not n.startswith("_")]
        self.assertEqual(exported, [], "H7: unscoped read paths exported from pol.core: %s" % exported)

    def test_hidden_candidate_flag_only_when_notices_opted_in(self):
        from pol.view import explain_for
        st = genesis("T1", {"u1": "human", "u2": "human"}, {"r": ["u1", "u2"]}, ["u1"])
        hr = dict(LAB, acl=[["hr"]])
        st, _ = run(st, dict(type="propose", id="p", idem="p", req="r", principal="u1", value="A", based_on=0))
        st, _ = run(st, dict(type="decide", id="a", idem="a", req="r", principal="u1", verb="accept", proposal="p", channel="owner_ui", based_on=0))
        st, _ = run(st, dict(type="supersede", id="s", idem="s", req="r", principal="u2", value="secret", channel="owner_cli", based_on=0, label=hr))
        out = explain_for(st, "r", Principal("ivan", "T1", frozenset(), 0), Policy(1, grants=GR))           # conflict_notice defaults to none
        self.assertNotIn("restricted_candidate_exists", out.get("contested") or {}, "H7: hidden-candidate existence shown with conflict_notice=none")

    def test_core_state_holds_no_plaintext_value(self):
        import json
        st = genesis("T1", {"u1": "human"}, {"r": ["u1"]}, ["u1"])
        st, _ = run(st, dict(type="supersede", id="s", idem="s", req="r", principal="u1", value="salary=250k", channel="owner_ui", based_on=0))
        self.assertNotIn("salary=250k", json.dumps(st), "H7: the decision core keeps the value in plaintext, so subject erasure cannot cover it")


class H8(unittest.TestCase):
    def test_default_outbox_rereads_source_label(self):
        alice = Principal("alice", "t1", frozenset({"finance"}), 3, True, region="EU")
        bob2 = Principal("bob2", "t1", frozenset({"finance"}), 3, region="EU")
        store = {"doc": Record("doc", "LAYOFFS", Label(tenants=["t1"], acl=[["finance"]]))}
        got = []
        ob = Outbox(Enforcer(lambda: POL, directory={"alice": alice, "bob2": bob2}.get, clock=lambda: 1), {"alice": alice, "bob2": bob2}.get,
                    "send_inbox", lambda p, b, c: got.append(b[0].text))
        ob.enqueue("alice", [Block("LAYOFFS", store["doc"].label)], {}, "bob2", record_ids=["doc"])
        store["doc"] = Record("doc", "LAYOFFS", Label(tenants=["t1"], acl=[["board"]]))
        ob.deliver_all()
        self.assertEqual(got, [], "H8: the default Outbox delivered after the source was narrowed to board (no label re-read)")

    def test_recipient_checked_is_recipient_sent_under_a_racing_directory(self):
        """Real threads: a flipper thread changes carol's directory entry between lookups. The object the sink receives must be the
        object the check allowed (one snapshot)."""
        alice = Principal("alice", "t1", frozenset({"finance"}), 3, True, region="EU")
        good = Principal("carol", "t1", frozenset({"finance"}), 3, True, region="EU")
        bad = Principal("carol", "t1", frozenset({"interns"}), 0, False, region="US", model="m_us")
        state = {"carol": good, "ver": 0}
        lock = threading.Lock()
        flip_req, flip_done, stop = threading.Event(), threading.Event(), threading.Event()

        def flipper():
            while not stop.is_set():
                if flip_req.wait(0.05):
                    flip_req.clear()
                    with lock:
                        state["carol"] = bad if state["carol"] is good else good
                        state["ver"] += 1
                    flip_done.set()
        th = threading.Thread(target=flipper, daemon=True)
        th.start()
        n_lookup = {"n": 0}

        def directory(pid):
            if pid == "alice":
                return alice
            with lock:
                cur = state["carol"]
            n_lookup["n"] += 1
            if n_lookup["n"] % 2 == 1:                 # after the first lookup of each delivery, ask the flipper to change the entry
                flip_done.clear()
                flip_req.set()
                flip_done.wait(1.0)
            return cur
        secret = Label(tenants=["t1"], acl=[["finance"]], sensitivity=3)
        violations = overlaps = 0
        runs = 200
        for i in range(runs):
            with lock:
                state["carol"] = good
            sent = []
            enf = Enforcer(lambda: POL, directory=directory, clock=lambda: 1)
            ob = Outbox(enf, directory, "send_inbox", lambda p, b, c: sent.append(c.get("recipient")))
            n_lookup["n"] = 0
            v0 = state["ver"]
            try:
                ob.queue.append({"sender": "alice", "blocks": [Block("PAYROLL", secret)], "ctx": {}, "recipient": "carol", "record_ids": []})
                ob.deliver_all()
            except PolicyDenied:
                pass
            overlaps += state["ver"] != v0
            for r in sent:
                if r is not None and "finance" not in r.groups:
                    violations += 1
        stop.set()
        self.assertEqual(violations, 0, "H8: %d of %d deliveries sent PAYROLL to a recipient object other than the one checked (overlaps %d)" % (violations, runs, overlaps))


class M1(unittest.TestCase):
    def test_event_principal_comes_from_session_not_body(self):
        st = genesis("T1", {"u1": "human", "uX": "human", "u2": "human"}, {"r": ["u1", "u2"]}, ["u1"])
        # uX (no admin) sends an event body that names the admin u1 as principal; nothing authenticates the body
        st, c = apply(st, {"type": "revoke", "id": "rv", "idem": "rv", "tenant": "T1", "principal": "u1", "target": "u2"})
        self.assertNotEqual(c, "revoked", "M1: an event body that names the admin as principal revoked u2; no session binds the principal")


class M3(unittest.TestCase):
    def test_lookalike_revoked_ids(self):
        leaks = []
        for rid in ("mаllory", "mallorý"):
            if allowed(lambda: decide(Principal(rid, "t1", frozenset(), 3), "read", Label(tenants=["t1"]), {}, policy=Policy(1, grants=GR, revoked=frozenset({"mallory"})))):
                leaks.append(rid)
        self.assertEqual(leaks, [], "M3: look-alike ids of revoked 'mallory' were allowed: %r" % leaks)

    def test_family_ban_covers_suffixed_and_tagged_variants(self):
        leaks = []
        for pin in ("gpt-x_2025", "gpt-x@2025-01", "gpt-x:latest"):
            P = Policy(1, grants=GR, destinations={"d": Dest("model", None, True, 3, model=pin)}, banned_models=frozenset({"gpt-x"}))
            if allowed(lambda: decide(BOB, "send_model", Label(tenants=["t1"]), {"destination": "d"}, policy=P, trusted={"now": 1})):
                leaks.append(pin)
        self.assertEqual(leaks, [], "M3: ban on family gpt-x did not cover %r" % leaks)

    def test_alias_chain_followed_and_cycle_refused(self):
        P = parse_policy({"version": 1, "grants": {"t1": ["send_model"]}, "banned_models": ["vendor-model-c"],
                          "model_aliases": {"alias-a": "alias-b", "alias-b": "vendor-model-c"},
                          "destinations": {"d": {"kind": "model", "region": None, "local": True, "max_sensitivity": 3, "model": "alias-a"}}})
        chain = allowed(lambda: decide(BOB, "send_model", Label(tenants=["t1"]), {"destination": "d"}, policy=P, trusted={"now": 1}))
        cyc = False
        try:
            parse_policy({"version": 1, "grants": {}, "model_aliases": {"a": "b", "b": "a"}})
        except PolicyError:
            cyc = True
        self.assertFalse(chain or not cyc, "M3: alias chain a->b->c with c banned allowed=%s; alias cycle refused=%s" % (chain, cyc))

    def test_in_code_policy_aliases_canonicalised(self):
        P2 = Policy(1, grants=GR, destinations={"d": Dest("model", None, True, 3, model="Alias-A")}, banned_models=frozenset({"real-model"}),
                    model_aliases={"Alias-A": "real-model"})
        self.assertFalse(allowed(lambda: decide(BOB, "send_model", Label(tenants=["t1"]), {"destination": "d"}, policy=P2, trusted={"now": 1})),
                         "M3: an in-code Policy alias key 'Alias-A' was not canonicalised, so the ban on its target did not apply")


if __name__ == "__main__":
    unittest.main()
