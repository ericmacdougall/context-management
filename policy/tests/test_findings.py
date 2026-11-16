"""One test class per review finding, against the rebuilt API (the red reproducers at 77ea895 are tests/red/)."""
import inspect
import json
import threading
import time
import unittest

from pol import _core, _pdp
from pol.decisions import DecisionService, MemoryEngine
from pol.directory import Directory, DirectoryError, Session, SessionAuthority
from pol.enforce import AuditLog, Enforcer, Outbox, PolicyDenied, Store
from pol.ids import IdError, canon
from pol.labels import Label
from pol.policy import Policy, PolicyError, parse_policy
from tests.kit import FixedClock, L, P, T, World, policy_doc


class N1_TrustedInputs(unittest.TestCase):
    def test_enforcer_requires_every_dependency(self):
        w = World()
        sig = inspect.signature(Enforcer)
        self.assertTrue(all(p.default is inspect.Parameter.empty for p in sig.parameters.values()))
        self.assertNotIn("clock", sig.parameters)
        for args in [(None, w.dir, w.sessions, w.audit), (lambda: w.pol, None, w.sessions, w.audit), (lambda: w.pol, w.dir, None, w.audit),
                     (lambda: w.pol, w.dir, w.sessions, None), (lambda: w.pol, object(), w.sessions, w.audit)]:
            with self.assertRaises(TypeError):
                Enforcer(*args)
        with self.assertRaises(TypeError):
            Store(None)

    def test_failing_or_absent_directory_entry_denies(self):
        w = World()
        rid = w.store.append(w.s("alice"), "x", L())

        class Boom(Directory):
            def lookup(self, pid):
                raise RuntimeError("down")
        s = w.s("alice")
        good = w.dir
        w.enf._dir = Boom([])
        with self.assertRaises(PolicyDenied) as e:
            w.enf._check(s, "read", [L()], {})
        self.assertTrue(e.exception.decision.reason.startswith("directory_error"))
        w.enf._dir = good
        w.dir.remove("alice")
        with self.assertRaises(PolicyDenied):
            w.store.read(s, rid)

    def test_caller_cannot_choose_principal_or_clock(self):
        w = World()
        rid = w.store.append(w.s("alice"), "secret", L(sensitivity=3))
        forged = Session(T, "alice", "owner_ui", "tok")            # hand-built: not issued
        for bad in (forged, P("alice"), None, "alice"):
            with self.assertRaises(PolicyDenied):
                w.store.read(bad, rid)
        for fn in (Store.read, Store.append, Outbox.enqueue, DecisionService.submit, DecisionService.explain):
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(params & {"now", "clock", "trusted", "principal", "viewer", "tenant"}, fn)

    def test_runtime_clock_decides_expiry(self):
        doc = policy_doc(authorizations={"au1": {"principal": "alice", "tenant": T, "programme": "prog", "expires": 1000.0}})
        doc["destinations"]["restricted-llm"] = {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-local",
                                                 "programme": "prog", "tenants": [T]}
        w = World(principals=[P("alice", restricted_ok=True)], policy=parse_policy(doc),
                  core_config={"tenant": T, "principals": {"alice": "human"}, "authority": {"r1": ["alice"]}, "admins": []})
        s = w.s("alice")
        lab = L(restricted=True)
        with FixedClock(w.enf, 999.0):
            w.enf._check(s, "send_model", [lab], {"destination": "restricted-llm", "authorization": "au1"})
        with FixedClock(w.enf, 1001.0):
            with self.assertRaises(PolicyDenied) as e:
                w.enf._check(s, "send_model", [lab], {"destination": "restricted-llm", "authorization": "au1"})
        self.assertEqual(e.exception.decision.reason, "restricted_authorization_missing")


class N2_Proposals(unittest.TestCase):
    def test_role_required_and_caps(self):
        w = World()
        self.assertEqual(w.propose(w.s("carol"), "c1", "x")["code"], "unauthorized")           # no role on r1
        self.assertEqual(w.propose(w.s("ann"), "a1", "x")["code"], "proposed")               # proposer role
        a = w.s("alice")
        codes = [w.propose(a, "p%d" % i, "v")["code"] for i in range(_core.MAX_OPEN_PER_PRINCIPAL + 1)]
        self.assertEqual(codes[-1], "proposal_cap")
        self.assertEqual(codes.count("proposed"), _core.MAX_OPEN_PER_PRINCIPAL)

    def test_per_requirement_cap(self):
        names = ["h%d" % i for i in range(20)]
        cfg = {"tenant": T, "principals": {n: "human" for n in names}, "authority": {"r1": names}, "admins": []}
        st = _core.MemoryState(cfg)
        lab = L().to_dict()
        n = 0
        last = None
        for who in names:
            for i in range(_core.MAX_OPEN_PER_PRINCIPAL):
                ev = {"type": "propose", "id": "%s/%d" % (who, i), "idem": "%s-%d" % (who, i), "tenant": T, "principal": who, "channel": "owner_ui",
                      "req": "r1", "value_ref": "v", "label": lab, "based_on": 0}
                last, _o, d = _core.reduce(st, ev)
                st.apply_delta(d)
                n += last == "proposed"
        self.assertEqual(n, _core.MAX_OPEN_PER_REQ)
        self.assertEqual(last, "proposal_cap")

    def test_cost_per_event_is_bounded_up_to_20000_proposals(self):
        """Deterministic witness: view calls and delta ops per event stay constant (proposals are rows/dict entries; nothing scans or
        copies all of them). 20,000 proposals are created and closed (propose by an analyst, reject by an agent)."""
        cfg = {"tenant": T, "principals": {"x": "analyst", "bot": "agent"}, "authority": {"r1": ["bot"]}, "proposers": {"r1": ["x"]}, "admins": []}
        st = _core.MemoryState(cfg)

        class Counting:
            def __init__(self, inner):
                self.inner, self.n = inner, 0
                self.tenant = inner.tenant

            def __getattr__(self, k):
                f = getattr(self.inner, k)

                def g(*a):
                    self.n += 1
                    return f(*a)
                return g
        lab = L().to_dict()
        worst, t_blocks, t0 = 0, [], time.perf_counter()
        for i in range(20000):
            ev = {"type": "propose", "id": "x/%d" % i, "idem": "p%d" % i, "tenant": T, "principal": "x", "channel": "agent_api", "req": "r1",
                  "value_ref": "v%d" % i, "label": lab, "based_on": 0}
            cv = Counting(st)
            c, _o, d = _core.reduce(cv, ev)
            self.assertEqual(c, "proposed")
            st.apply_delta(d)
            ev2 = {"type": "decide", "id": "bot/%d" % i, "idem": "r%d" % i, "tenant": T, "principal": "bot", "channel": "agent_api", "req": "r1",
                   "verb": "reject", "proposal": "x/%d" % i, "proposal_hash": _core.proposal_hash("x/%d" % i, "v%d" % i, "x"), "based_on": 0}
            cv2 = Counting(st)
            c2, _o, d2 = _core.reduce(cv2, ev2)
            self.assertEqual(c2, "decided_reject")
            st.apply_delta(d2)
            worst = max(worst, cv.n + len(d), cv2.n + len(d2))
            if (i + 1) % 2000 == 0:
                t1 = time.perf_counter()
                t_blocks.append(t1 - t0)
                t0 = t1
        self.assertLessEqual(worst, 20)
        self.assertEqual(len(st.props["r1"]), 20000)
        self.assertEqual(st.heads["r1"]["version"], 0)             # N3: agent rejects never moved the head
        self.assertLess(max(t_blocks[-3:]) / min(t_blocks[:3]), 3.0, t_blocks)


class N3_HeadStarvation(unittest.TestCase):
    def test_non_human_reject_never_moves_head_and_human_reader_succeeds_first_try(self):
        w = World()
        a, b, ann = w.s("alice"), w.s("bot"), w.s("ann")
        mine = w.propose(a, "mine", "keep")["id"]
        for i in range(12):
            h = w.svc.explain(a, "r1")["version"]                 # the human reads the head ...
            junk = w.propose(ann, "j%d" % i, "junk")["id"]
            self.assertEqual(w.decide(b, "rej%d" % i, "reject", junk, h)["code"], "decided_reject")   # ... the agent churns ...
            self.assertEqual(w.svc.explain(a, "r1")["version"], h)                                       # ... the head did not move
        self.assertEqual(w.decide(a, "acc", "accept", mine, h)["code"], "decided_accept")


class H2_CredentialKeys(unittest.TestCase):
    def test_only_exact_ascii_keys(self):
        w = World()
        pol = w.pol
        p = P("alice")
        ok = _pdp.evaluate(p, "read", [L()], {"credential": {"cap_clearance": 1}}, pol, 0.0, None)
        self.assertTrue(ok.allow, ok)
        for k in ("cаp_actions", "caṕ_actions", "scope", "allowed_actions", "caps_actions", "CAP_ACTIONS", "cap_actions ", 7):
            d = _pdp.evaluate(p, "read", [L()], {"credential": {k: ["read"]}}, pol, 0.0, None)
            self.assertEqual((d.allow, d.reason), (False, "attr_unrecognised_key"), k)
        d = _pdp.evaluate(p, "read", [L()], {"credential": {}, "tenant": T}, pol, 0.0, None)
        self.assertEqual(d.reason, "forged_request")


class H6_IdSquatting(unittest.TestCase):
    def test_ids_are_namespaced_and_store_assigned(self):
        w = World()
        a, b = w.s("alice"), w.s("bob")
        rb = w.propose(b, "dec-1", "b's")
        ra = w.propose(a, "dec-1", "a's")
        self.assertEqual((ra["code"], rb["code"]), ("proposed", "proposed"))
        self.assertNotEqual(ra["id"], rb["id"])
        self.assertEqual(w.svc.submit(a, {"type": "retract", "id": "bob/x", "client_id": "x", "idem": "x", "req": "r1", "based_on": 0})["code"],
                         "identity_in_body")
        self.assertEqual(w.svc.submit(a, {"type": "retract", "client_id": "../bob", "idem": "x", "req": "r1", "based_on": 0})["code"], "bad_event")
        r1 = w.store.append(a, "x", L())
        r2 = w.store.append(b, "x", L())
        self.assertNotEqual(r1, r2)


class H7_ScopedReadsAndErasure(unittest.TestCase):
    def test_hidden_flag_only_when_opted_in(self):
        for notice, expect in (("none", None), ("existence_only", True)):
            w = World(policy=parse_policy(policy_doc(conflict_notice=notice)))
            w.propose(w.s("alice"), "p", "top secret", sensitivity=3)
            view = w.svc.explain(w.s("carol"), "r2")                     # carol: clearance 0, participant of r2 only
            self.assertEqual(view.get("hidden_candidates"), None if notice == "none" else False)
            w.svc.submit(w.s("alice"), {"type": "supersede", "client_id": "s", "idem": "s", "req": "r2", "value": "zz-withheld-zz", "label": L(sensitivity=3).to_dict(),
                                       "based_on": 0})
            v = w.svc.explain(w.s("carol"), "r2")
            self.assertIsNone(v["value"])
            self.assertEqual(v.get("hidden_candidates"), expect)
            self.assertNotIn("zz-withheld-zz", json.dumps(v))
            self.assertIsNone(w.svc.explain(w.s("carol"), "r1"))           # not a participant: nothing at all

    def test_core_holds_refs_and_erasure_covers_what_others_said(self):
        w = World()
        a, b = w.s("alice"), w.s("bob")
        pa = w.propose(a, "p1", "alice says: dana is late", subjects=["dana"])
        w.decide(b, "d1", "accept", pa["id"], 0)
        w.propose(b, "p2", "bob says: dana again", subjects=["dana"])
        snap = json.dumps(w.engine._st.snapshot()) + json.dumps(w.engine._log)
        self.assertNotIn("dana is late", snap)
        out = w.svc.erase_subject(w.s("admin"), "dana")
        self.assertEqual(out["erased"], 2)
        v = w.svc.explain(a, "r1")
        self.assertTrue(v["value"]["erased"])
        self.assertNotIn("dana", json.dumps(v))

    def test_legal_hold_queues_erasure(self):
        w = World(policy=parse_policy(policy_doc(hold_events=["hold-7"])))
        w.propose(w.s("alice"), "p1", "held", subjects=["dana"], holds=["hold-7"])
        w.propose(w.s("bob"), "p2", "free", subjects=["dana"])
        self.assertEqual(w.svc.erase_subject(w.s("admin"), "dana"), {"erased": 1, "queued": 1, "denied": 0, "refused": None})
        self.assertEqual(w.svc.erase_subject(w.s("alice"), "dana")["refused"], "unauthorized")


class H8_Outbox(unittest.TestCase):
    def test_no_skip_parameter(self):
        for fn in (Outbox.__init__, Outbox.enqueue, Outbox.deliver_all):
            self.assertFalse(set(inspect.signature(fn).parameters) & {"enqueue_only", "check_lineage", "skip", "recheck", "resolve", "recipient"})

    def test_narrowed_label_after_enqueue_is_refused(self):
        w = World()
        a = w.s("alice")
        rid = w.store.append(a, "x", L())
        sent = []
        ob = Outbox(w.enf, w.store, "send_inbox", lambda s, r, t, q: sent.append(t))
        ob.enqueue(a, [rid], {"recipient_id": "bob"})
        w.store._recs[rid] = w.store._recs[rid].__class__(rid, T, "x", L(acl=[["nobody"]]), ())   # label narrowed after enqueue
        _d, refused = ob.deliver_all()
        self.assertEqual(sent, [])
        self.assertIn(refused[0]["refusal"], ("acl", "recipient_acl"))

    def test_race_directory_change_against_delivery(self):
        """Real threads: a flipper toggles the recipient's clearance 3 <-> 0 while deliveries run. Every send must go to a recipient
        object the decision checked (identity), and that object must satisfy the label. Overlaps = deliveries during which the
        directory changed between the decision's lookup and the send."""
        import pol.enforce as E
        from exp.patches import control
        sends, overlaps, bad = self._race(E)
        print("\nH8 race (production): sends=%d overlaps=%d bad=%d" % (sends, overlaps, bad))
        self.assertEqual(bad, 0)
        self.assertGreaterEqual(sends, 200)
        self.assertGreater(overlaps, 0)                    # the window was actually exercised
        sends, overlaps, bad = self._race(control("h8_second_lookup"))
        print("H8 race (control h8_second_lookup): sends=%d overlaps=%d bad=%d" % (sends, overlaps, bad))
        self.assertGreater(bad, 0)                         # the control (recipient looked up again for the send) is caught

    def _race(self, E):
        w = World()
        w.audit = E.AuditLog()
        w.enf = E.Enforcer(lambda: w.pol, w.dir, w.sessions, w.audit)
        w.store = E.Store(w.enf)
        Outbox_ = E.Outbox
        a = w.s("alice")
        rid = w.store.append(a, "secret", L(sensitivity=2))
        checked, bad, overlaps, sends = [], [], [0], [0]
        real = E.evaluate

        def spy(principal, action, labels, request, policy, now, recipient):
            d = real(principal, action, labels, request, policy, now, recipient)
            checked.append((recipient, w.dir.version(), d.allow))
            time.sleep(0.0003)                             # widen the decision-to-send window
            return d

        def sink(sender, rcp, texts, req):
            sends[0] += 1
            r, v0, allow = checked[-1]
            if rcp is not r or not allow or rcp.clearance < 2:
                bad.append(rcp)
            if w.dir.version() != v0:
                overlaps[0] += 1
        E.evaluate = spy
        stop = threading.Event()

        def flip():
            hi = True
            while not stop.is_set():
                w.dir.put(P("bob", clearance=3 if hi else 0))
                hi = not hi
                time.sleep(0)
        th = threading.Thread(target=flip)
        th.start()
        try:
            for _ in range(5000):
                if sends[0] >= 200:
                    break
                ob = Outbox_(w.enf, w.store, "send_inbox", sink)
                try:
                    ob.enqueue(a, [rid], {"recipient_id": "bob"})
                except E.PolicyDenied:
                    continue
                ob.deliver_all()
        finally:
            stop.set()
            th.join()
            E.evaluate = real
        return sends[0], overlaps[0], len(bad)


class M1_IdentityFromSession(unittest.TestCase):
    def test_forged_body(self):
        w = World()
        a = w.s("alice")
        for k, v in (("tenant", "tenant-b"), ("principal", "admin"), ("channel", "owner_cli")):
            r = w.svc.submit(a, {"type": "revoke", "client_id": "r", "idem": "r" + k, "target": "bob", k: v})
            self.assertEqual(r["code"], "identity_in_body")
        self.assertEqual(w.svc.submit(a, {"type": "revoke", "client_id": "r", "idem": "r", "target": "bob"})["code"], "unauthorized")
        self.assertEqual(w.svc.submit(w.s("admin"), {"type": "revoke", "client_id": "r", "idem": "r", "target": "bob"})["code"], "revoked")
        self.assertIn("identity_in_body", [r["reason"] for r in w.audit.rows()])


class M2_RetryOutcome(unittest.TestCase):
    def test_memory_retry_returns_original(self):
        w = World()
        a = w.s("alice")
        body = {"type": "supersede", "client_id": "s1", "idem": "same", "req": "r1", "value": "v", "label": L().to_dict(), "based_on": 0}
        self.assertEqual(w.svc.submit(a, body)["code"], "superseded")
        r = w.svc.submit(a, body)
        self.assertEqual((r["code"], r["original"]), ("duplicate", "superseded"))


class M3_Ids(unittest.TestCase):
    def test_confusables_and_combining_marks(self):
        for bad in ("аlice", "álice", "alicé", "al ice", "", "x" * 200):
            with self.assertRaises(IdError):
                canon(bad)
        self.assertEqual(canon("ALICE"), "alice")
        self.assertEqual(canon("ａｌｉｃｅ"), "alice")            # NFKC width folding
        d = Directory([P("mallory")])
        with self.assertRaises(DirectoryError):
            d.put(P("rnallory"))                                   # partial skeleton: looks like an existing id
        pol = parse_policy(policy_doc(revoked=["mallory"]))
        self.assertEqual(_pdp.evaluate(P("rnallory"), "read", [L()], {}, pol, 0.0, None).reason, "principal_revoked")   # over-match

    def test_family_ban_over_and_under_match(self):
        pol = parse_policy(policy_doc(banned_models=["gpt-x"]))
        for m in ("gpt-x", "gpt-x_2025", "gpt-x@2025-01", "gpt-x:latest"):
            self.assertTrue(any(pol.spec_matches(b, m) for b in pol.banned_models), m)
        self.assertFalse(any(pol.spec_matches(b, "gpt-xl") for b in pol.banned_models))          # other family: not covered
        pol2 = parse_policy(policy_doc(banned_models=["gpt-x@2025"]))
        self.assertTrue(pol2.spec_matches("gpt-x@2025", "gpt-x_2025"))
        self.assertFalse(pol2.spec_matches("gpt-x@2025", "gpt-x@2025-01"))
        doc = policy_doc()
        doc["models"]["gptx-evil"] = {"family": "gpt-x", "version": "9"}                       # id must start with its family + separator
        with self.assertRaises(PolicyError):
            parse_policy(doc)
        doc = policy_doc()
        doc["destinations"]["cloud"]["model"] = "undeclared-9"
        d = _pdp.evaluate(P("alice"), "send_model", [L()], {"destination": "cloud"}, parse_policy(doc), 0.0, None)
        self.assertEqual(d.reason, "destination_model_undeclared")
        d = _pdp.evaluate(P("alice"), "send_model", [L()], {"destination": "cloud"}, pol, 0.0, None)
        self.assertEqual(d.reason, "model_banned")

    def test_alias_fixed_point_cycle_and_canonical(self):
        pol = parse_policy(policy_doc(model_aliases={"Alias-A": "alias-b", "alias-b": "gpt-x_2025"}))
        self.assertEqual(pol.resolve_model("alias-a"), "gpt-x_2025")
        with self.assertRaises(PolicyError):
            parse_policy(policy_doc(model_aliases={"a1": "a2", "a2": "a1"}))
        with self.assertRaises(PolicyError):
            Policy(version=1, model_aliases={"a1": "nowhere"})
        p = Policy(version=1, models={"gpt-x": {"family": "gpt-x", "version": "1"}}, model_aliases={"ALIAS": "gpt-x"})
        self.assertEqual(dict(p.model_aliases), {"alias": "gpt-x"})


class Audit_Refusals(unittest.TestCase):
    def test_every_refusal_is_audited(self):
        w = World()
        a, b = w.s("alice"), w.s("bot")
        codes = [w.svc.submit(b, {"type": "supersede", "client_id": "s", "idem": "s", "req": "r1", "value": "v", "label": L().to_dict(),
                                  "based_on": 0})["code"],
                 w.svc.submit(a, {"type": "retract", "client_id": "s", "idem": "q", "req": "r9", "based_on": 0})["code"],
                 w.svc.submit(a, {"type": "retract", "client_id": "s", "idem": "q2", "req": "r1", "based_on": 5})["code"]]
        self.assertEqual(codes, ["agent_not_permitted", "unknown_req", "stale"])
        denied = [r["reason"] for r in w.audit.rows() if r.get("action") == "decision" and not r["allow"]]
        self.assertEqual(denied, codes)
        self.assertEqual(len(w.vault._vals), 0)          # refused value text is not kept


if __name__ == "__main__":
    unittest.main()
