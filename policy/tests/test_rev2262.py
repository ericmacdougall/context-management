"""Review of 2262eaa (research/policy-review-2262eaa.md), in-memory findings. Each test runs UNMODIFIED against 2262eaa and fails there
by a behavioural assertion unless its docstring says it is a regression guard (red log in policy/results/red/).

H2  DecisionService.explain and .table never asked the revocation source: a principal revoked by a core `revoke` event, or listed as
    revoked in the policy document, kept reading heads (dc.explain refuses it: dc._explain_raw returns nothing for a revoked
    principal). Fixed: the entry judgment and the final judgment (_still) of both reads ask Enforcer._revocations."""
import threading
import time
import unittest

from pol.policy import parse_policy
from tests.kit import World, policy_doc


def body(w, cid, text, req="r1"):
    return {"type": "propose", "client_id": cid, "idem": "k-" + cid, "req": req, "value": text, "label": w.lab(), "based_on": 0}


class H2_RevokedPrincipalReads(unittest.TestCase):
    def _reads(self, w, s):
        return w.svc.explain(s, "r1"), w.svc.table(s)

    def test_core_revoke_ends_reads(self):
        """Red at 2262eaa: after admin's applied revoke of alice, explain returned alice's view of r1 and table listed 2 reqs."""
        w = World()
        a, adm = w.s("alice"), w.s("admin")
        self.assertEqual(w.svc.submit(a, body(w, "c0", "seen"))["code"], "proposed")
        e, t = self._reads(w, a)
        self.assertIsNotNone(e, "positive control: alice reads before the revoke")
        self.assertTrue(t)
        self.assertEqual(w.svc.submit(adm, {"type": "revoke", "client_id": "rv", "idem": "rv", "target": "alice"})["code"], "revoked")
        self.assertEqual(self._reads(w, a), (None, []))
        self.assertIsNotNone(w.svc.explain(w.s("bob"), "r1"), "bob is not revoked and still reads")

    def test_policy_revocation_ends_reads(self):
        """Red at 2262eaa: alice listed as revoked in the policy document still read."""
        w = World()
        a = w.s("alice")
        self.assertIsNotNone(w.svc.explain(a, "r1"), "positive control")
        w.pol = parse_policy(policy_doc(revoked=["alice"]))
        self.assertEqual(self._reads(w, a), (None, []))

    def test_revocation_during_the_wait(self):
        """The final judgment: explain judged alice, then waited for the engine lock; the revocation lands during the wait (policy
        document, so no engine lock is needed). Red at 2262eaa: the view came back."""
        for name in ("explain", "table"):
            with self.subTest(name):
                w = World()
                a = w.s("alice")
                call = (lambda: w.svc.explain(a, "r1")) if name == "explain" else (lambda: w.svc.table(a))
                self.assertTrue(call(), "positive control")
                out = {}
                w.engine._lock.acquire()
                t = threading.Thread(target=lambda: out.__setitem__("r", call()))
                try:
                    t.start()
                    time.sleep(0.3)
                    self.assertTrue(t.is_alive(), "vacuous: did not wait")
                    w.pol = parse_policy(policy_doc(revoked=["alice"]))
                finally:
                    w.engine._lock.release()
                t.join(5)
                self.assertFalse(t.is_alive())
                self.assertFalse(out.get("r"), "%r" % (out,))


if __name__ == "__main__":
    unittest.main()
