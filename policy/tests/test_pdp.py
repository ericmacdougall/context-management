import unittest

from exp.gen import base_policy
from pol.enforce import Block, Enforcer, GuardedChat, PolicyDenied, Record, Store, derive
from pol.labels import Label, join2, leq
from pol.pdp import Principal, decide
from pol.stubs import Network, StubChat

P = Principal("p", "t1", frozenset(["g1"]), 3, True, region="EU")


class T(unittest.TestCase):
    def setUp(self):
        self.pol = base_policy()

    def d(self, lab, action="send_model", **ctx):
        return decide(P, action, lab, ctx, policy=self.pol)

    def test_default_deny_unlabelled(self):
        self.assertEqual(self.d([]).reason, "unlabelled")
        self.assertFalse(self.d(None).allow)

    def test_dimensions(self):
        self.assertTrue(self.d(Label(["t1"]), destination="d_model_us").allow)
        self.assertEqual(self.d(Label(["t1"], regions=["EU"]), destination="d_model_us").reason, "region_forbidden")
        self.assertEqual(self.d(Label(["t1"], local_only=True), destination="d_model_us").reason, "local_inference_required")
        self.assertEqual(self.d(Label(["t1"], banned=["m3"]), destination="d_model_us").reason, "model_banned")
        self.assertEqual(self.d(Label(["t1"], models=["m1"]), destination="d_model_us").reason, "model_not_allowed")
        self.assertEqual(self.d(Label(["t2"]), destination="d_model_us").reason, "tenant_mismatch")
        self.assertEqual(self.d(Label(["t1"], acl=[["g9"]]), destination="d_model_us").reason, "acl")
        self.assertEqual(self.d(Label(["t1"], restricted=True), destination="d_model_cyber", now=10).reason, "restricted_authorization_missing")
        self.assertTrue(self.d(Label(["t1"], restricted=True), destination="d_model_cyber", now=10, authorization="auth1").allow)
        self.assertEqual(self.d(Label(["t1"]), destination="nope").reason, "unknown_destination")

    def test_credential_cannot_widen(self):
        r = self.d(Label(["t2"]), destination="d_model_us", credential={"tenant": "t2"})
        self.assertEqual(r.reason, "attr_clash")
        r = self.d(Label(["t1"], sensitivity=3), destination="d_model_us", credential={"cap_clearance": 1})
        self.assertEqual(r.reason, "clearance")

    def test_join_is_restrictive(self):
        a, b = Label(["t1"], regions=["EU", "US"]), Label(["t1"], regions=["EU"], local_only=True)
        j = join2(a, b)
        self.assertTrue(leq(a, j) and leq(b, j))
        self.assertEqual(j.regions, frozenset(["EU"]))

    def test_log_and_version_and_use_time(self):
        cur = [self.pol]
        enf = Enforcer(lambda: cur[0])
        net = Network()
        net.register("model", "send_model")
        chat = GuardedChat(StubChat(net, "d_model_us", "m3"), enf, "d_model_us")
        blk = [Block("x", Label(["t1"]))]
        chat.complete(P, blk)
        cur[0] = self.pol.tighten(banned_models=frozenset(["m3"]))
        with self.assertRaises(PolicyDenied):
            chat.complete(P, blk)
        self.assertEqual([e["policy_version"] for e in enf.log.entries], [1, 2])
        self.assertEqual([e["allow"] for e in enf.log.entries], [True, False])

    def test_pin_drift_dropped(self):
        enf = Enforcer(lambda: self.pol)
        net = Network()
        net.register("model", "send_model")
        chat = GuardedChat(StubChat(net, "d_model_us", "m3", reported_model="m9"), enf, "d_model_us")
        with self.assertRaises(PolicyDenied) as e:
            chat.complete(P, [Block("x", Label(["t1"]))])
        self.assertEqual(e.exception.decision.reason, "pin_drift")

    def test_store_refuses_derived_below_lineage(self):
        enf = Enforcer(lambda: self.pol)
        st = Store(enf)
        hi = Record("a", "secret", Label(["t1"], sensitivity=2, regions=["EU"]))
        st.append(P, hi, {"region": "EU"})
        low = Record("b", "summary", Label(["t1"]), ("a",), "summary")
        with self.assertRaises(PolicyDenied) as e:
            st.append(P, low, {"region": "EU"})
        self.assertEqual(e.exception.decision.reason, "label_below_lineage")


if __name__ == "__main__":
    unittest.main()
