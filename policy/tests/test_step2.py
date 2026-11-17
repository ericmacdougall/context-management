"""Step 2 (authenticated sessions as the single root of identity), in-process side. Run first against e15241c (the review-3 fixes,
before step 2); the red output is in the policy owner's work dir (red-artifacts/step2_*.out).

  S2-A  actor and trust_origin are DERIVED from the session (principal kind and channel), stamped on every event the service builds,
        and a body that names either is refused identity_in_body like tenant, principal, channel and id.
        Vocabulary (ASSUMPTION, from COMPOSITION.md items 1 and 3): a human on an owner channel is actor "user", trust_origin
        "user_typed"; any other principal is actor "assistant", trust_origin "agent_generated".
  S2-L  look-alike principal ids (pol.ids.skeleton) cannot both be configured in one tenant's decision core: the core config is refused,
        so the database install (which builds its principals from the same config) refuses it too.
Stdlib only; no database."""
import unittest

from pol import _core
from tests.kit import T, World


class DerivedOrigin(unittest.TestCase):
    def _log(self, w):
        return w.engine._log

    def test_human_event_is_stamped_user_typed(self):
        w = World()
        out = w.propose(w.s("alice"), "a1", "x")
        self.assertEqual(out["code"], "proposed")
        ev = self._log(w)[-1]
        self.assertEqual((ev.get("actor"), ev.get("trust_origin")), ("user", "user_typed"))

    def test_agent_event_is_stamped_agent_generated(self):
        w = World()
        out = w.propose(w.s("bot"), "b1", "x")
        self.assertEqual(out["code"], "proposed")
        ev = self._log(w)[-1]
        self.assertEqual((ev.get("actor"), ev.get("trust_origin")), ("assistant", "agent_generated"))

    def test_body_naming_actor_or_trust_origin_is_refused(self):
        w = World()
        s = w.s("bot")
        for k, v in (("actor", "user"), ("trust_origin", "user_typed")):
            body = {"type": "propose", "client_id": "f-" + k, "idem": "k-" + k, "req": "r1", "value": "x", "label": w.lab(), "based_on": 0, k: v}
            out = w.svc.submit(s, body)
            self.assertEqual(out["code"], "identity_in_body", k)
        self.assertEqual(self._log(w), [])

    def test_derivation_table(self):
        self.assertEqual(_core.origin("human", "owner_ui"), ("user", "user_typed"))
        self.assertEqual(_core.origin("human", "owner_cli"), ("user", "user_typed"))
        for kind in ("agent", "analyst"):
            self.assertEqual(_core.origin(kind, "agent_api"), ("assistant", "agent_generated"))
        self.assertEqual(_core.origin("human", "agent_api"), ("assistant", "agent_generated"))    # never user_typed off an owner channel


class LookAlikes(unittest.TestCase):
    def test_core_config_with_look_alike_principals_is_refused(self):
        for a, b in (("alice", "a1ice"), ("alice", "aiice"), ("bob", "b0b"), ("mary", "rnary")):
            cfg = {"tenant": T, "principals": {a: "human", b: "human"}, "authority": {"r1": [a]}, "proposers": {}, "admins": []}
            with self.assertRaises(Exception, msg="%s / %s accepted" % (a, b)):
                _core.MemoryState(cfg)

    def test_distinct_ids_are_accepted(self):
        _core.MemoryState({"tenant": T, "principals": {"alice": "human", "bob": "human"}, "authority": {"r1": ["alice"]}, "proposers": {},
                           "admins": []})


if __name__ == "__main__":
    unittest.main()
