"""Round-3 review findings (research/policy-review-515e594.md, "Review of the rescue at 80bb6a7" and its follow-up), reproduced first.

Each test was run against 80bb6a7 before any fix (artifact agent-ad21c9e102633fab2/artifacts/red_r3_80bb6a7.out) and failed there for
the reason in its docstring. Postgres findings (R3-7, R3-8) are in tests/test_pg.py; the surface (R3-9) is in tests/test_surface.py.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import unicodedata
import unittest

from pol.decisions import DecisionService, MemoryEngine
from pol.enforce import Enforcer, GuardedChat, PolicyDenied
from pol.labels import Label
from pol.policy import PolicyError, parse_policy
from tests.kit import L, P, T, World, policy_doc


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception:           # noqa: BLE001
        return True
    return False


class R3_1_Sessions(unittest.TestCase):
    def test_issue_without_a_credential_is_refused(self):
        """80bb6a7: SessionAuthority.issue(principal_id, channel) mints a live admin session from a bare id (p8 A2)."""
        w = World()
        self.assertTrue(_raises(w.sessions.issue, "admin", "owner_ui"))
        self.assertTrue(_raises(w.sessions.issue, "admin"))

    def test_channel_is_derived_from_the_credential(self):
        """80bb6a7: the caller picks the channel, so one human holds both owner channels (p8 A5)."""
        params = set(inspect.signature(type(World().sessions).issue).parameters)
        self.assertNotIn("channel", params)
        self.assertFalse(params & {"principal", "principal_id", "pid"})

    def test_no_decision_without_a_session(self):
        """80bb6a7: Enforcer._decide(principal_id, tenant, ...) allows with no session at all (p8 A3)."""
        w = World()
        lab = L()
        for name, fn in inspect.getmembers(w.enf, callable):
            if name.startswith("__"):
                continue
            try:
                inspect.signature(fn)
            except (TypeError, ValueError):     # builtins (the runtime clock) take no principal
                continue
            n = len([p for p in inspect.signature(fn).parameters.values() if p.default is p.empty and p.kind == p.POSITIONAL_OR_KEYWORD])
            for args in (["alice", T, "read", [lab], {}, None, None][:n], ["alice"] * n):
                try:
                    out = fn(*args)
                except Exception:           # noqa: BLE001  refusing is the expected outcome
                    continue
                flat = out if isinstance(out, tuple) else (out,)
                self.assertFalse(any(getattr(x, "allow", False) is True for x in flat), "%s decided without a session" % name)


class R3_2_SubjectSpellings(unittest.TestCase):
    def _w(self):
        return World()

    def test_erasure_covers_case_and_whitespace_spellings(self):
        """80bb6a7: erasing 'alice smith' leaves the 'Alice Smith' value and reports success (p8 B1)."""
        w = self._w()
        a, b = w.s("alice"), w.s("bob")
        w.propose(a, "p1", "one", subjects=["Alice Smith"])
        w.propose(b, "p2", "two", subjects=["alice smith"])
        w.propose(b, "p3", "three", subjects=["  ALICE\tSmith "])
        out = w.svc.erase_subject(w.s("admin"), "alice smith")
        self.assertEqual(out["erased"], 3)
        self.assertEqual([x for x in w.svc.explain(a, "r1")["open"] if x["value"] is not None and not x["value"].get("erased")], [])

    def test_erasure_covers_unicode_normal_forms(self):
        """80bb6a7: a value labelled 'José Ruiz' in NFD survives erasure by the NFC spelling, which reports erased 0 (p8 B2, p9)."""
        w = self._w()
        w.propose(w.s("alice"), "p1", "about jose", subjects=[unicodedata.normalize("NFD", "José Ruiz")])
        self.assertEqual(w.svc.erase_subject(w.s("admin"), unicodedata.normalize("NFC", "José Ruiz"))["erased"], 1)

    def test_nothing_found_is_distinct_from_success(self):
        """80bb6a7: erasing an unknown subject returns the same shape as success, refused None."""
        w = self._w()
        out = w.svc.erase_subject(w.s("admin"), "nobody at all")
        self.assertEqual(out.get("result"), "no_values_found")


class R3_3_SubjectTokens(unittest.TestCase):
    def test_tokens_are_unconfirmable_after_erasure(self):
        """80bb6a7: the token is HMAC(vault key, [subject, tenant, name]); it survives erasure and the key holder confirms a guess (p8 B3-B6)."""
        w = World()
        a = w.s("alice")
        w.propose(a, "p1", "about dana", subjects=["dana"])
        tok = w.engine._log[-1]["label"]["subjects"][0]
        self.assertEqual(w.svc.erase_subject(w.s("admin"), "dana")["erased"], 1)
        # the key holder recomputes the deterministic token for the guessed name: must NOT match
        key = getattr(w.vault, "_key", b"")
        guess = "subj-" + hmac.new(key, json.dumps(["subject", T, "dana"]).encode(), hashlib.sha256).hexdigest()[:32]
        self.assertNotEqual(guess, tok)
        # no vault structure still links the token to anything, and a re-appearing subject gets a fresh token
        self.assertNotIn(tok, repr(vars(w.vault)))
        self.assertNotEqual(w.vault._event_label(T, L(subjects=["dana"])).to_dict()["subjects"][0], tok)


class R3_4_Caps(unittest.TestCase):
    def test_non_humans_cannot_lock_a_human_out(self):
        """80bb6a7: 16 analysts x 16 open proposals fill MAX_OPEN_PER_REQ; alice (authority) then gets proposal_cap (p8 C1-C3)."""
        ns = ["n%02d" % i for i in range(17)]
        cfg = {"tenant": T, "principals": dict({"alice": "human", "admin": "human"}, **{n: "analyst" for n in ns}),
               "authority": {"r1": ["alice"]}, "proposers": {"r1": ns}, "admins": ["admin"]}
        w = World(principals=[P("alice"), P("admin")] + [P(n, kind="analyst") for n in ns], core_config=cfg)
        for n in ns[:16]:
            s = w.s(n)
            for i in range(16):
                self.assertEqual(w.propose(s, "x%d" % i, "v")["code"], "proposed")
        self.assertEqual(w.propose(w.s(ns[16]), "x0", "v")["code"], "proposal_cap")
        self.assertEqual(w.propose(w.s("alice"), "h0", "human value")["code"], "proposed")


class R3_5_AgentsOnlyProposeAndRemark(unittest.TestCase):
    """ASSUMPTION (coordinator's relay of the reviewer's safe default; not yet confirmed by the user)."""

    def test_agent_cannot_accept_or_reject(self):
        """80bb6a7: the agent with decide rights accepts a human proposal, and its reject closes proposals before a human sees them (p8 C4)."""
        w = World()
        a, bot = w.s("alice"), w.s("bot")
        w.propose(a, "p1", "alice value")
        w.propose(a, "p2", "alice value 2")
        self.assertEqual(w.decide(bot, "d1", "accept", "alice/p1", 0)["code"], "agent_not_permitted")
        self.assertEqual(w.decide(bot, "d2", "reject", "alice/p2", 0)["code"], "agent_not_permitted")
        self.assertEqual(sorted(x["id"] for x in w.svc.explain(a, "r1")["open"]), ["alice/p1", "alice/p2"])
        self.assertEqual(w.svc.explain(a, "r1")["version"], 0)


class R3_6_Models(unittest.TestCase):
    def test_floating_versions_are_refused_at_parse(self):
        """80bb6a7: parse_policy accepts gpt-x:latest with version 'latest' (p8 E1)."""
        for mid, ver in (("gpt-x:latest", "latest"), ("gpt-x", "latest"), ("gpt-x", "stable"), ("gpt-x", "*"), ("gpt-x", ""),
                         ("gpt-x-preview", "current")):
            doc = policy_doc(models={"m-local": {"family": "m-local", "version": "1"}, mid: {"family": "gpt-x", "version": ver}})
            doc["destinations"] = {"local": doc["destinations"]["local"]}
            self.assertTrue(_raises(parse_policy, doc), (mid, ver))

    def test_pin_drift_disables_the_destination(self):
        """80bb6a7: each drifted answer is dropped, but the next request is sent to the same destination again."""
        w = World(principals=[P("alice", model="cloud")])
        calls = []

        class Drifting:
            def complete(self, msgs, system=None):
                calls.append(1)
                return type("R", (), {"text": "x", "model": "gpt-xl"})()

        rid = w.store.append(w.s("alice"), "hello", L())
        chat = GuardedChat(Drifting(), w.enf, w.store, "cloud")
        for _ in range(2):
            with self.assertRaises(PolicyDenied):
                chat.complete(w.s("alice"), [rid])
        self.assertEqual(len(calls), 1)


class R3_10_OneRevocation(unittest.TestCase):
    def test_core_revoke_reaches_the_store(self):
        """80bb6a7: a principal revoked by a core revoke event can still read through the Store (two revocation lists)."""
        w = World()
        rid = w.store.append(w.s("bob"), "bob note", L())
        bob = w.s("bob")
        self.assertEqual(w.svc.submit(w.s("admin"), {"type": "revoke", "client_id": "rv1", "idem": "rv1", "target": "bob"})["code"], "revoked")
        with self.assertRaises(PolicyDenied):
            w.store.read(bob, rid)

    def test_policy_revoke_reaches_the_core(self):
        """80bb6a7: a principal revoked in the policy document can still submit decisions."""
        w = World(policy=parse_policy(policy_doc(revoked=["bob"])))
        out = w.propose(w.s("bob"), "p1", "v")
        self.assertIn("revoked", out["code"])


if __name__ == "__main__":
    unittest.main()
