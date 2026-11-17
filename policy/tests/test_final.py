"""Final policy review (reviews/policy-final/review-1.md, run 1356898b, at a2d65fd), in-memory findings, reproduced first. Each test
runs UNMODIFIED against a2d65fd (faults are injected by wrapping existing callables; no name introduced by the fix is imported) and
failed there for the behavioural reason in its docstring unless the docstring says it is a regression guard (red logs in
policy/results/red/). P2 (the S2 verifier) is in tests/test_verify_s2.py; P1 and P4 follow in their own commit.

P3  an exception between allocating a value reference and the engine's decision left the value and its idempotency mapping in the
    vault, so the next valid request with that key was accepted under a fresh reference and its exact retry was refused
    idem_conflict. Fixed: the event shape check is total, the service validates the type and key before allocating, the vault stages a
    value without publishing the retry mapping and settles it (publish on acceptance, delete on refusal or failure before acceptance;
    acceptance after a lost response is read back from the engine's idempotency record by fingerprint)."""
from __future__ import annotations

import json
import random
import unittest

from pol import _core
from tests.kit import T, World


class Injected(RuntimeError):
    pass


def body(w, cid, text, idem="k-same", typ="propose", req="r1", **lab):
    return {"type": typ, "client_id": cid, "idem": idem, "req": req, "value": text, "label": w.lab(**lab), "based_on": 0}


def outcome(fn):
    try:
        return fn()["code"]
    except Exception as e:          # noqa: BLE001  the observation, not a handler
        return "raised:" + type(e).__name__


def applied_refs(w):
    return {e.get("value_ref") for e in w.engine._log if e.get("value_ref")}


class P3_MalformedThenValid(unittest.TestCase):
    def test_malformed_type_then_valid_then_exact_retry(self):
        """a2d65fd (reviewer's probe): `"type": []` passes the service's checks, mints a value, then _core._shape raises TypeError
        (a list tested against the REQUIRED dict); the value and the retry mapping stay. The next valid request with the same key and
        another text is proposed under a fresh reference, and its exact retry is refused idem_conflict (stored values 2)."""
        w = World()
        a = w.s("alice")
        c0 = outcome(lambda: w.svc.submit(a, dict(body(w, "c0", "orphan"), type=[])))
        left = (len(w.vault._vals), len(w.vault._idem))
        good = body(w, "c1", "kept")
        r1 = w.svc.submit(a, good)
        r2 = w.svc.submit(a, dict(good))
        got = (c0, left, r1["code"], r2["code"], r2["value_ref"] == r1["value_ref"], len(w.vault._vals))
        self.assertEqual(got, ("bad_event", (0, 0), "proposed", "duplicate", True, 1))

    def test_other_malformed_keys_never_leave_a_value(self):
        """The class: whatever is malformed (type, idem, req, based_on, a field of the wrong JSON type), the submission is refused
        bad_event without raising and leaves no value and no mapping. a2d65fd: red for an unhashable type ({}); the other cases are
        regression guards (refused by the core after allocation, then cleaned up)."""
        bad = [dict(type={}), dict(type=None), dict(type=1), dict(idem=[]), dict(idem={}), dict(idem=""), dict(idem=None),
               dict(req=[]), dict(based_on="0"), dict(based_on=[0]), dict(based_on=-1), dict(type="decide", verb=[]),
               dict(type="supersede", based_on=0.5)]
        for i, over in enumerate(bad):
            with self.subTest(over=json.dumps(over)):
                w = World()
                a = w.s("alice")
                self.assertEqual(outcome(lambda: w.svc.submit(a, dict(body(w, "m%d" % i, "x%d" % i), **over))), "bad_event")
                self.assertEqual((len(w.vault._vals), len(w.vault._idem)), (0, 0))
                good = body(w, "ok%d" % i, "after")
                r1, r2 = w.svc.submit(a, good), w.svc.submit(a, dict(good))
                self.assertEqual((r1["code"], r2["code"], r2["value_ref"] == r1["value_ref"]), ("proposed", "duplicate", True))


class P3_FaultInjection(unittest.TestCase):
    """A fault at each step of a submission with a value. Before acceptance: the exception reaches the caller, and the vault holds
    neither the value nor a mapping; the next valid request with the same key is proposed and its exact retry is duplicate with its
    reference. After acceptance (the engine applied the event, then something failed: the caller never saw the answer): the value
    and the mapping stay, and the exact retry is duplicate with the reference the engine logged."""

    def _world(self):
        w = World()
        return w, w.s("alice")

    def _after_pre_fault(self, w, a, exc):
        self.assertTrue(exc.startswith("raised:"), exc)
        self.assertEqual((len(w.vault._vals), len(w.vault._idem), len(w.engine._log)), (0, 0, 0), "a value or mapping outlived the fault")
        good = body(w, "c2", "second")
        r1, r2 = w.svc.submit(a, good), w.svc.submit(a, dict(good))
        self.assertEqual((r1["code"], r2["code"], r2["value_ref"] == r1["value_ref"], len(w.vault._vals)), ("proposed", "duplicate", True, 1))

    def test_fault_while_tokenising_the_label(self):
        """a2d65fd: the value and the mapping were published by _mint before _event_label ran: both outlive the fault."""
        w, a = self._world()
        real = w.vault._event_label

        def boom(*x, **k):
            raise Injected("event label")
        w.vault._event_label = boom
        exc = outcome(lambda: w.svc.submit(a, body(w, "c1", "first", subjects=["dana"])))
        w.vault._event_label = real
        self._after_pre_fault(w, a, exc)

    def test_fault_in_the_engine_before_it_applies(self):
        """a2d65fd: the engine raises before deciding (any engine or connection failure): value and mapping outlive it."""
        w, a = self._world()
        real = w.engine._submit

        def boom(*x, **k):
            raise Injected("engine down")
        w.engine._submit = boom
        exc = outcome(lambda: w.svc.submit(a, body(w, "c1", "first")))
        w.engine._submit = real
        self._after_pre_fault(w, a, exc)

    def test_fault_inside_the_reduction(self):
        """a2d65fd: _core.reduce raises inside the engine lock (the reviewer's TypeError, here injected for a well-formed event)."""
        w, a = self._world()
        real = _core.reduce

        def boom(*x, **k):
            raise Injected("reduce")
        _core.reduce = boom
        try:
            exc = outcome(lambda: w.svc.submit(a, body(w, "c1", "first")))
        finally:
            _core.reduce = real
        self._after_pre_fault(w, a, exc)

    def test_fault_while_applying_the_delta(self):
        """a2d65fd: apply_delta raises before changing anything (e.g. MemoryError): value and mapping outlive it."""
        w, a = self._world()
        st = w.engine._st
        real = st.apply_delta

        def boom(delta):
            if delta:
                raise Injected("apply")
            return real(delta)
        st.apply_delta = boom
        exc = outcome(lambda: w.svc.submit(a, body(w, "c1", "first")))
        st.apply_delta = real
        self._after_pre_fault(w, a, exc)

    def _after_post_fault(self, w, a, exc, sent):
        self.assertTrue(exc.startswith("raised:"), exc)
        self.assertEqual(len(w.engine._log), 1, "the event was applied")
        logged = w.engine._log[0]["value_ref"]
        self.assertEqual(set(w.vault._vals), {logged}, "the accepted value was deleted")
        r = w.svc.submit(a, dict(sent))
        self.assertEqual((r["code"], r["value_ref"]), ("duplicate", logged), "the accepted submission is no longer retryable")

    def test_fault_after_the_engine_applied(self):
        """Regression guard (a2d65fd keeps the value here): the engine applied the event and then failed (lost response)."""
        w, a = self._world()
        real = w.engine._submit

        def after(*x, **k):
            real(*x, **k)
            raise Injected("lost response")
        w.engine._submit = after
        sent = body(w, "c1", "first")
        exc = outcome(lambda: w.svc.submit(a, sent))
        w.engine._submit = real
        self._after_post_fault(w, a, exc, sent)

    def test_fault_in_the_audit_after_acceptance(self):
        """Regression guard: the audit write after acceptance fails."""
        w, a = self._world()
        real = w.enf._audit.add

        def boom(entry):
            if entry.get("action") == "decision" and entry.get("allow"):
                raise Injected("audit")
            return real(entry)
        w.enf._audit.add = boom
        sent = body(w, "c1", "first")
        exc = outcome(lambda: w.svc.submit(a, sent))
        w.enf._audit.add = real
        self._after_post_fault(w, a, exc, sent)


class P3_Totality(unittest.TestCase):
    JUNK = ([], {}, None, 0, 1, -1, 1.5, True, False, "", "x", ["a"], {"a": 1}, [[]], {"k": []})
    KEYS = ("type", "id", "idem", "tenant", "principal", "channel", "req", "value_ref", "label", "based_on", "verb", "proposal",
            "proposal_hash", "pending", "choose", "target", "role")

    def test_reduce_never_raises(self):
        """a2d65fd: _core.reduce raised TypeError for an unhashable `type` (and the service let it escape after allocating). The core
        must answer bad_event (or a decision) for ANY event, never raise; Postgres's dc._shape answers bad_event for the same input."""
        rng = random.Random(16096)
        types = list(_core.REQUIRED) + list(self.JUNK)
        cfg = {"tenant": T, "principals": {"alice": "human", "admin": "human"}, "authority": {"r1": ["alice"]}, "admins": ["admin"]}
        failures = []
        for i in range(3000):
            st = _core.MemoryState(cfg)
            ev = {"type": rng.choice(types), "id": "alice/x%d" % i, "idem": "k%d" % i, "tenant": T, "principal": "alice", "channel": "owner_ui"}
            for k in rng.sample(self.KEYS, rng.randint(1, len(self.KEYS))):
                ev[k] = rng.choice(self.JUNK + ("r1", "accept", 0, T, "alice"))
            try:
                code, _o, _d = _core.reduce(st, ev)
                if code not in _core.OUTCOME_CODES:
                    failures.append((repr(ev), "code " + code))
            except Exception as e:      # noqa: BLE001
                failures.append((repr(ev), type(e).__name__))
        self.assertEqual(failures[:5], [], "%d of 3000 events made reduce raise or answer outside OUTCOME_CODES" % len(failures))


class P3_VaultInvariant(unittest.TestCase):
    def test_random_mix_keeps_the_vault_exact(self):
        """After every submission of a random mix (valid and malformed bodies, shared keys, texts and labels, retries): every stored
        value is the value_ref of an applied event, and every retry mapping names a stored value of an applied event with that key.
        a2d65fd: malformed types left orphan values and mappings."""
        rng = random.Random(61)
        w = World()
        sess = {p: w.s(p) for p in ("alice", "bob")}
        bad = []
        for i in range(400):
            pid = rng.choice(("alice", "bob"))
            b = body(w, "c%d" % rng.randrange(40), "t%d" % rng.randrange(4), idem="k%d" % rng.randrange(6),
                     subjects=rng.choice(([], ["dana"], ["erin"])))
            r = rng.random()
            if r < 0.15:
                b["type"] = rng.choice(([], {}, 3, None))
            elif r < 0.25:
                b["based_on"] = rng.choice(("0", [0], -1))
            outcome(lambda: w.svc.submit(sess[pid], b))
            logged = applied_refs(w)
            orphans = sorted(set(w.vault._vals) - logged)
            stale = sorted(k for k, ref in w.vault._idem.items() if ref not in w.vault._vals or ref not in logged)
            if orphans or stale:
                bad.append((i, len(orphans), len(stale)))
        self.assertEqual(bad[:5], [], "%d submissions left the vault inexact (step, orphan values, stale mappings)" % len(bad))


if __name__ == "__main__":
    unittest.main()
