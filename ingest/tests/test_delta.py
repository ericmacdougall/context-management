"""Tests for the response to review 2 (PREREG-amend-3). Written BEFORE the fixes and run against 0801866 behaviour to record failing-first
(ingest/results3/failing-first.txt). One class per finding."""
import json
import os
import random
import shutil
import tempfile
import unittest

from ingest.controls import SharedOmissionIngester
from ingest.ingester import Ingester
from ingest.proofs_common import HostLedger, event_tuples, repeated_conversation, scripted_conversation
from ingest.stubs import text
from ingest.testkit import Rig


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ingdelta-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sub(self, name):
        p = os.path.join(self.d, name)
        os.makedirs(p)
        return p

    def user_events(self, rig):
        return [e for e in rig.events() if e["prov"] == "user"]


class F1Control(unittest.TestCase):
    def test_four_layer_control_disables_all_four_layers(self):
        from ingest import proof_items_123 as p
        from ingest import run_proofs
        got = {}
        orig = p.item2_race_threads
        p.item2_race_threads = lambda out, **kw: (got.update(kw), {})[1]
        try:
            run_proofs.REG["2bctl4"]("unused")
        finally:
            p.item2_race_threads = orig
        for layer in ("fence", "unique", "state_guard", "chain"):
            self.assertIs(got.get(layer), False, "the 4-layer control leaves %s enabled" % layer)


class F2Oracle(unittest.TestCase):
    def test_complete_redaction_predicate_is_separate_from_detection(self):
        """amend-3 version of this test pinned the frozen scanner's key-body leak (complete_redaction 0 of 5). amend-4 repaired the scanner,
        so the same example now redacts completely; the predicate must still report detection and complete redaction separately."""
        from ingest import heldout
        r = heldout._catch({"private_key_header": heldout.HELDOUT["private_key_header"]}, 5, 9)["per_variant"]["private_key_header"]
        self.assertEqual((r.get("detected"), r.get("complete_redaction")), (5, 5))


class F3Repeats(Base):
    def _check(self, provider, pattern, window, n=20, boundary=None):
        """amend-4 changed the contract: repeated-content hosts are exact only when they declare new_count (boundary); unique-text hosts
        need no declaration."""
        if boundary is None:
            boundary = pattern != "unique"
        rig, led = repeated_conversation(Rig, self.sub("%s-%s-%d" % (provider, pattern, window)), provider, pattern, window, n, boundary=boundary)
        rig.sync()
        self.assertEqual(len(self.user_events(rig)), n, "%s %s window %d" % (provider, pattern, window))
        self.assertEqual(event_tuples(rig.events()), led.expected())
        self.assertEqual([g["kind"] for g in rig.ing.gaps(rig.stream_id) if g["blocking"]], [])
        return rig

    def test_same_text_windows(self):
        for provider in ("anthropic", "openai"):
            for window in (4, 6):
                self._check(provider, "same", window)

    def test_alternating_and_cycle_windows(self):
        for provider in ("anthropic", "openai"):
            for pattern in ("alt", "tri"):
                for window in (4, 6, 8):
                    self._check(provider, pattern, window)

    def test_unique_text_windows_carry_no_ambiguity_flag(self):
        for provider in ("anthropic", "openai"):
            for window in (4, 8):
                rig = self._check(provider, "unique", window)
                self.assertEqual(rig.ing.completeness(rig.stream_id)["ambiguous_calls"], [])
                self.assertEqual([g for g in rig.ing.gaps(rig.stream_id, open_only=False) if g["kind"] == "ambiguous_alignment"], [])

    def test_repeats_are_reported_not_hidden(self):
        rig, led = repeated_conversation(Rig, self.sub("nb"), "anthropic", "same", 4, 20)          # no boundary declared
        rig.sync()
        self.assertTrue(rig.ing.completeness(rig.stream_id)["ambiguous_calls"])
        self.assertFalse(rig.ing.claim_exactly_once(rig.stream_id))
        self.assertGreaterEqual(len(self.user_events(rig)), 20)

    def test_stateless_host_two_identical_turns_both_ingested(self):
        """yes, then yes again, from a host that resends only the newest message. Indistinguishable from a retry by content."""
        for provider in ("anthropic", "openai"):
            rig = Rig(self.sub(provider), provider, [{"content": [text("ok")]} if provider == "anthropic" else {"message": {"role": "assistant", "content": "ok"}}] * 2)
            for _ in range(2):
                rig.msgs[:] = [{"role": "user", "content": "yes"}]
                rig.call(rig.request())
            rig.sync()
            self.assertEqual(len(self.user_events(rig)), 2, provider)
            flagged = [e for e in self.user_events(rig) if json.loads(e["meta"]).get("ambiguous_repeat")]
            self.assertEqual([e["call_n"] for e in flagged], [2])
            self.assertEqual(rig.ing.completeness(rig.stream_id)["ambiguous_calls"], [2])


class F3RetryIdentity(Base):
    def _rig(self, n=2):
        return Rig(self.d, "anthropic", [{"content": [text("a%d" % i)]} for i in range(1, n + 1)])

    def test_undeclared_identical_resend_is_a_flagged_duplicate(self):
        rig = self._rig()
        rig.msgs.append({"role": "user", "content": "hello"})
        rig.call(rig.request())
        rig.call(rig.request())
        rig.sync()
        self.assertEqual(len(self.user_events(rig)), 2)
        self.assertEqual([bool(json.loads(e["meta"]).get("ambiguous_repeat")) for e in self.user_events(rig)], [False, True])
        self.assertEqual(rig.ing.completeness(rig.stream_id)["ambiguous_calls"], [2])

    def test_declared_retry_suppresses_request_events_visibly(self):
        rig = self._rig()
        rig.msgs.append({"role": "user", "content": "hello"})
        rig.call(rig.request())
        rig.tap.call(rig.request(), retry_of=1)
        rig.sync()
        self.assertEqual(len(self.user_events(rig)), 1)
        self.assertEqual(len([e for e in rig.events() if e["prov"] == "assistant"]), 2)
        port = [e["kind"] for e in rig.events(include_port=True) if e["prov"] == "port"]
        self.assertIn("declared_retry", port)
        self.assertEqual(rig.ing.completeness(rig.stream_id)["ambiguous_calls"], [])

    def test_false_retry_claim_is_ignored_and_recorded(self):
        rig = self._rig()
        rig.msgs.append({"role": "user", "content": "hello"})
        o = rig.call(rig.request())
        rig.msgs.append({"role": "assistant", "content": o["content"]})
        rig.msgs.append({"role": "user", "content": "something else"})
        rig.tap.call(rig.request(), retry_of=1)
        rig.sync()
        self.assertEqual(len(self.user_events(rig)), 2)
        port = [e["kind"] for e in rig.events(include_port=True) if e["prov"] == "port"]
        self.assertIn("retry_claim_ignored", port)
        self.assertNotIn("declared_retry", port)


class F4Oracle(Base):
    def test_shared_omission_passes_baseline_comparison_but_fails_the_oracle(self):
        rig = scripted_conversation(Rig, self.sub("c"), 30, seed=103)
        recs = rig.log.read(rig.stream_id)
        rnd = random.Random(5)
        deliveries = [r for r in recs for _ in range(rnd.randint(1, 3))]
        rnd.shuffle(deliveries)
        base = SharedOmissionIngester(os.path.join(self.d, "b.db"), rig.log.blob)
        base.deliver(recs)
        base.drain(rig.stream_id)
        arm = SharedOmissionIngester(os.path.join(self.d, "a.db"), rig.log.blob)
        for i in range(0, len(deliveries), 5):
            arm.deliver(deliveries[i:i + 5])
            arm.drain(rig.stream_id)
        self.assertEqual([(e["ikey"], e["content"]) for e in arm.events(rig.stream_id)], [(e["ikey"], e["content"]) for e in base.events(rig.stream_id)])
        self.assertNotEqual(event_tuples(arm.events(rig.stream_id)), rig.ledger.expected())
        good = Ingester(os.path.join(self.d, "g.db"), rig.log.blob)
        good.deliver(recs)
        good.drain(rig.stream_id)
        self.assertEqual(event_tuples(good.events(rig.stream_id)), rig.ledger.expected())

    def test_delivery_item_reports_oracle_match_per_run(self):
        from ingest import proof_items_123 as p
        old = p.SEEDS
        p.SEEDS = [101, 102]
        try:
            s = p.item2_delivery(self.sub("out"), tag="t_delivery")
        finally:
            p.SEEDS = old
        self.assertEqual(s["oracle_identical"], 2)


if __name__ == "__main__":
    unittest.main()
