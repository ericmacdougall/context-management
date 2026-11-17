"""Review-3 findings on the in-memory path (reviews/policy/review-3.md, run a883d987, at 0a20b64), reproduced first.

Each test was run against 0a20b64 before its fix (artifacts in the policy owner's work dir, red-artifacts/) and failed there for the
reason in its docstring. Postgres findings (N1, N3, N4, N5 in dc.submit, the anchor) are in tests/test_review3_pg.py; N2 is
tests/test_sync_guard.py; N7 is tests/test_surface.py (test_ast_fallback_agrees_with_runtime_enumeration)."""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from tests.kit import L, T, World


def _old_derivation(key: bytes, principal: str, idem: str, text: str, subject: str) -> str:
    """The 0a20b64 value reference: HMAC(vault key, [tenant, principal, idem, text, label with the subject NAME])."""
    msg = json.dumps([T, principal, idem, text, L(subjects=[subject]).to_dict()], sort_keys=True, separators=(",", ":")).encode()
    return "val-" + hmac.new(key, msg, hashlib.sha256).hexdigest()[:40]


class N5_ValueRefs(unittest.TestCase):
    def test_value_ref_cannot_be_confirmed_by_guessing_the_subject_after_erasure(self):
        """0a20b64: _value_ref HMACs the label, subject NAME included, under the vault key that survives erasure; the key holder, with
        the logged principal and idempotency key and a known text, recomputes the reference for a guessed name and it matches only for
        the erased subject. After the fix no retained key reproduces it and the vault keeps nothing that mentions the reference."""
        w = World()
        out = w.propose(w.s("alice"), "p1", "the client's file", subjects=["dana"])
        ref = out["value_ref"]
        self.assertEqual(out["code"], "proposed")
        self.assertEqual(w.svc.erase_subject(w.s("admin"), "dana")["erased"], 1)
        keys = [v for v in vars(w.vault).values() if isinstance(v, (bytes, bytearray))]
        right = [k for k in keys if _old_derivation(bytes(k), "alice", "k-p1", "the client's file", "dana") == ref]
        wrong = [k for k in keys if _old_derivation(bytes(k), "alice", "k-p1", "the client's file", "eve") == ref]
        self.assertEqual(len(wrong), 0)                               # counts only: never print key bytes
        self.assertEqual(len(right), 0, "a key the vault retains after erasure confirms the erased subject's name")
        self.assertNotIn(ref, repr(vars(w.vault)), "the vault still holds something that mentions the erased value's reference")

    def test_retry_gets_the_stored_reference_while_the_value_exists(self):
        """M2 kept: the same body retried with the same idempotency key is a duplicate carrying the original reference; one value stored."""
        w = World()
        a = w.s("alice")
        r1 = w.propose(a, "p1", "x", subjects=["dana"])
        r2 = w.propose(a, "p1", "x", subjects=["dana"])
        self.assertEqual((r2["code"], r2["value_ref"]), ("duplicate", r1["value_ref"]))
        self.assertEqual(len(w.vault._vals), 1)
        r3 = w.propose(a, "p1", "other text", subjects=["dana"])
        self.assertEqual((r3["code"], r3["value_ref"]), ("idem_conflict", None))
        self.assertEqual(len(w.vault._vals), 1)

    def test_retry_after_erasure_stores_nothing_and_leaks_nothing(self):
        """After erasure a retry of the erased submission is an idem_conflict (fresh reference, fresh subject token), and its value is
        not kept: the answer is the same whatever subject name the retry guesses."""
        w = World()
        a = w.s("alice")
        w.propose(a, "p1", "x", subjects=["dana"])
        w.svc.erase_subject(w.s("admin"), "dana")
        codes = {name: w.propose(a, "p1", "x", subjects=[name])["code"] for name in ("dana", "eve")}
        self.assertEqual(codes, {"dana": "idem_conflict", "eve": "idem_conflict"})
        self.assertEqual(len(w.vault._vals), 0)

    def test_references_are_not_a_function_of_the_value(self):
        """Two principals storing the same text and label get unrelated references (no deterministic derivation to test guesses with)."""
        w = World()
        r1 = w.propose(w.s("alice"), "p1", "same", subjects=["dana"])["value_ref"]
        r2 = w.propose(w.s("bob"), "p1", "same", subjects=["dana"])["value_ref"]
        self.assertNotEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
