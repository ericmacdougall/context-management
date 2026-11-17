"""Delta review 1 at d05d731 (reviews/policy-delta/review-1.md, run ad27751f), in-memory findings, reproduced first.

Each test was run against d05d731 before its fix and failed there for the reason in its docstring (logs in policy/results/red/).
Postgres findings (M1, M3 on dc.submit) are in tests/test_delta1_pg.py; M2 (the S2 verifier) is in tests/test_verify_s2.py."""
from __future__ import annotations

import threading
import unittest

from tests.kit import T, World


class M3_OverlappingIdempotency(unittest.TestCase):
    def _body(self, w, cid, text):
        return {"type": "propose", "client_id": cid, "idem": "k-same", "req": "r1", "value": text, "label": w.lab(subjects=["dana"]),
                "based_on": 0}

    def test_overlapping_conflicting_payloads_keep_the_winner_retryable(self):
        """d05d731: Vault._mint publishes the retry mapping for A before the engine decides. B (same principal and idempotency key,
        other payload) is accepted while the mapping still names A; A is refused idem_conflict and deletes its reference AND the
        mapping; a retry of the accepted B then mints a fresh reference and is refused idem_conflict instead of duplicate. After the
        fix, allocation, acceptance and cleanup are serialised per (tenant, principal, idempotency key): whichever submission wins,
        its retry is duplicate with its original reference, and exactly one value is stored."""
        w = World()
        a = w.s("alice")
        eng = w.svc._engines[T]
        real = eng._submit
        a_in, go = threading.Event(), threading.Event()

        def gated(ev, *a, **k):             # final review P1: the engine also takes the caller's final gate
            if ev.get("id") == "alice/cA":
                a_in.set()
                go.wait(5)
            return real(ev, *a, **k)

        eng._submit = gated
        out = {}
        try:
            ta = threading.Thread(target=lambda: out.__setitem__("A", w.svc.submit(a, self._body(w, "cA", "text A"))))
            ta.start()
            self.assertTrue(a_in.wait(5), "A never reached the engine")
            tb = threading.Thread(target=lambda: out.__setitem__("B", w.svc.submit(a, self._body(w, "cB", "text B"))))
            tb.start()
            tb.join(0.5)                 # d05d731: B completes while A is paused; fixed: B waits for A's key and then is decided
            go.set()
            ta.join(5)
            tb.join(5)
        finally:
            go.set()
            eng._submit = real
        self.assertFalse(ta.is_alive() or tb.is_alive(), "a submission is stuck")
        self.assertEqual(sorted([out["A"]["code"], out["B"]["code"]]), ["idem_conflict", "proposed"])
        win = "A" if out["A"]["code"] == "proposed" else "B"
        retry = w.svc.submit(a, self._body(w, "c" + win, "text " + win))
        self.assertEqual((retry["code"], retry["value_ref"]), ("duplicate", out[win]["value_ref"]),
                         "the accepted submission is no longer retryable (winner %s)" % win)
        self.assertEqual(len(w.vault._vals), 1)

    def test_losers_reference_is_deleted_and_the_winners_kept(self):
        """The loser's value is never kept and the winner's value is: the vault holds exactly the winner's reference."""
        w = World()
        a = w.s("alice")
        r1 = w.svc.submit(a, self._body(w, "c1", "first"))
        r2 = w.svc.submit(a, self._body(w, "c2", "second"))
        self.assertEqual((r1["code"], r2["code"]), ("proposed", "idem_conflict"))
        self.assertEqual(set(w.vault._vals), {r1["value_ref"]})
        r3 = w.svc.submit(a, self._body(w, "c1", "first"))
        self.assertEqual((r3["code"], r3["value_ref"]), ("duplicate", r1["value_ref"]))


if __name__ == "__main__":
    unittest.main()
