"""Tests for the response to the delta review (PREREG-amend-4). Written BEFORE the fixes; failing-first is recorded in results4/failing-first.txt.
Classification of each test (amend-4 A4): behavioural = fails on add7c86 product code for the stated reason; api = fails there only because
the field or keyword does not exist yet; control = demonstrates a control and is not a regression test.
  N1 test_reviewer_case_without_boundary_is_flagged_and_blocks        behavioural
  N1 test_reviewer_case_with_boundary_is_exact                         api (new_count)
  N1 test_multi_new_messages_without_boundary_loses_nothing_and_blocks behavioural
  N1 test_multi_new_messages_with_boundary_is_exact                    api (new_count)
  N1 test_undeclared_resend_blocks_exactly_once_claim                  behavioural
  N1 test_declared_calls_are_visible_in_completeness_and_events        api (declared_calls)
  N1 test_unique_text_hosts_stay_clean                                 behavioural guard (passes before and after; must keep passing)
  N2 test_payload_corruption_with_ids_preserved_is_caught              behavioural
  F2 test_private_key_block_is_fully_redacted                          behavioural
  F2 test_truncated_key_block_body_is_redacted                         behavioural
  F2 test_token_and_separator_formats_are_redacted                     behavioural
"""
import json
import os
import random
import shutil
import tempfile
import unittest

from ingest.canon import Hasher
from ingest.controls import CorruptPayloadIngester
from ingest.ingester import Ingester
from ingest.proofs_common import event_tuples, repeated_conversation, scripted_conversation
from ingest.scan import ReferenceScanner, redact_text
from ingest.stubs import text
from ingest.testkit import Rig
from collections import Counter

PEM_H = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_F = "-----END " + "RSA PRIVATE KEY-----"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ingdelta2-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sub(self, name):
        p = os.path.join(self.d, name)
        os.makedirs(p)
        return p

    def rig(self, n=3, prov="anthropic"):
        spec = (lambda t: {"content": [text(t)]}) if prov == "anthropic" else (lambda t: {"message": {"role": "assistant", "content": t}})
        return Rig(self.sub("r%d" % random.randint(0, 10 ** 9)), prov, [spec("ok") for _ in range(n)])

    def users(self, rig):
        return [json.loads(e["content"])["text"] for e in rig.events() if e["prov"] == "user"]


class N1Alignment(Base):
    def _reviewer_case(self, **kw):
        rig = self.rig(2)
        rig.msgs.append({"role": "user", "content": "yes"})
        o = rig.call(rig.request())
        rig.msgs.append({"role": "assistant", "content": o["content"]})
        # two newly authored messages, the first has the same content as an earlier one
        rig.msgs[:] = [{"role": "user", "content": "yes"}, {"role": "user", "content": "new"}]
        rig.call(rig.request(), **kw)
        rig.sync()
        return rig

    def test_reviewer_case_without_boundary_is_flagged_and_blocks(self):
        rig = self._reviewer_case()
        self.assertEqual(sorted(self.users(rig)), ["new", "yes", "yes"])
        flagged = [e for e in rig.events() if e["prov"] == "user" and json.loads(e["meta"]).get("ambiguous_repeat")]
        self.assertTrue(flagged)
        st = rig.stream_id
        self.assertTrue([g for g in rig.ing.gaps(st) if g["blocking"] and g["kind"] == "ambiguous_alignment"])
        self.assertFalse(rig.ing.claim_complete(st, rig.log.head(st)[0]))
        self.assertFalse(rig.ing.claim_exactly_once(st))

    def test_reviewer_case_with_boundary_is_exact(self):
        rig = self._reviewer_case(new_count=2)
        self.assertEqual(sorted(self.users(rig)), ["new", "yes", "yes"])
        st = rig.stream_id
        self.assertEqual([g for g in rig.ing.gaps(st) if g["blocking"]], [])
        self.assertTrue(rig.ing.claim_exactly_once(st))
        self.assertEqual(rig.ing.completeness(st)["declared_calls"], [2])

    def test_multi_new_messages_without_boundary_loses_nothing_and_blocks(self):
        for prov in ("anthropic", "openai"):
            for pattern in ("same", "alt"):
                for window in (6, 8):
                    rig, led = repeated_conversation(Rig, self.sub("%s%s%d" % (prov, pattern, window)), prov, pattern, window, 12, per_call=2)
                    rig.sync()
                    missing = Counter(map(tuple, led.expected())) - Counter(map(tuple, event_tuples(rig.events())))
                    self.assertEqual(sum(missing.values()), 0, "%s %s %d lost %r" % (prov, pattern, window, list(missing)[:3]))
                    self.assertFalse(rig.ing.claim_exactly_once(rig.stream_id))
                    self.assertTrue([g for g in rig.ing.gaps(rig.stream_id) if g["blocking"]])

    def test_multi_new_messages_with_boundary_is_exact(self):
        for prov in ("anthropic", "openai"):
            for pattern in ("same", "alt"):
                for window in (6, 8):
                    rig, led = repeated_conversation(Rig, self.sub("%s%s%d" % (prov, pattern, window)), prov, pattern, window, 12, per_call=2, boundary=True)
                    rig.sync()
                    self.assertEqual(event_tuples(rig.events()), led.expected(), (prov, pattern, window))
                    self.assertTrue(rig.ing.claim_exactly_once(rig.stream_id))

    def test_undeclared_resend_blocks_exactly_once_claim(self):
        rig = self.rig(2)
        rig.msgs.append({"role": "user", "content": "hello"})
        rig.call(rig.request())
        rig.call(rig.request())
        rig.sync()
        st = rig.stream_id
        self.assertEqual(self.users(rig), ["hello", "hello"])
        self.assertFalse(rig.ing.claim_exactly_once(st))
        self.assertFalse(rig.ing.claim_complete(st, rig.log.head(st)[0]))

    def test_declared_calls_are_visible_in_completeness_and_events(self):
        rig = self.rig(2)
        rig.msgs.append({"role": "user", "content": "hello"})
        rig.call(rig.request())
        rig.tap.call(rig.request(), retry_of=1)
        rig.sync()
        comp = rig.ing.completeness(rig.stream_id)
        self.assertEqual(comp["declared_calls"], [2])
        resp = [e for e in rig.events() if e["side"] == "resp" and e["call_n"] == 2]
        self.assertTrue(all(json.loads(e["meta"]).get("declared") for e in resp))

    def test_unique_text_hosts_stay_clean(self):
        for prov in ("anthropic", "openai"):
            rig, led = repeated_conversation(Rig, self.sub(prov), prov, "unique", 4, 20)
            rig.sync()
            self.assertEqual(event_tuples(rig.events()), led.expected())
            self.assertTrue(rig.ing.claim_exactly_once(rig.stream_id))
        rig = scripted_conversation(Rig, self.sub("scripted"), 30, seed=104)
        rig.sync()
        self.assertTrue(rig.ing.claim_exactly_once(rig.stream_id))


class N2Payload(Base):
    def test_payload_corruption_with_ids_preserved_is_caught(self):
        for prov in ("anthropic", "openai"):
            rig = scripted_conversation(Rig, self.sub(prov), 30, seed=103, provider=prov)
            recs = rig.log.read(rig.stream_id)
            arm = CorruptPayloadIngester(os.path.join(self.d, prov + ".db"), rig.log.blob)
            arm.deliver(recs)
            arm.drain(rig.stream_id)
            got, exp = event_tuples(arm.events(rig.stream_id)), rig.ledger.expected()
            self.assertEqual([t[:5] for t in got], [t[:5] for t in exp], "ids and text are preserved by the control")
            self.assertNotEqual(got, exp, "%s: payload corruption must be visible to the oracle" % prov)


class F2Scanner(unittest.TestCase):
    def _red(self, t):
        return redact_text(t, ReferenceScanner(), Hasher(b"k"))

    @staticmethod
    def _frag(s, out, k=8):
        return any(s[i:i + k] in out for i in range(len(s) - k + 1))

    def test_private_key_block_is_fully_redacted(self):
        r = random.Random(7)
        body = ["".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(64)) for _ in range(3)]
        t = "please keep this\n" + "\n".join([PEM_H] + body + [PEM_F]) + "\nthanks"
        out = self._red(t)
        self.assertEqual(sum(self._frag(b, out) for b in body), 0, 'body lines surviving')
        self.assertNotIn(PEM_H, out)
        self.assertIn("thanks", out)

    def test_truncated_key_block_body_is_redacted(self):
        r = random.Random(8)
        tok = "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(36))
        out = self._red("my credential is " + PEM_H + "\nMIIE" + tok + " thanks")
        self.assertFalse(self._frag("MIIE" + tok, out))

    def test_token_and_separator_formats_are_redacted(self):
        import base64
        r = random.Random(9)
        al = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        body = "".join(r.choice(al) for _ in range(24))
        for name, s in (("underscore_vendor_key", "sk_" + "live_" + body), ("source_host_token", "gh" + "p_" + "".join(r.choice(al) for _ in range(36))),
                        ("base32_vendor_key", base64.b32encode(("sk-" + "live-" + body).encode()).decode()),
                        ("dot_split_vendor_key", ".".join("sk-" + "live-" + body)), ("space_split_vendor_key", " ".join("sk-" + "live-" + body))):
            out = self._red("my credential is %s thanks" % s)
            self.assertFalse(self._frag(body, out), name)
            self.assertNotEqual(out, "my credential is %s thanks" % s, name)


if __name__ == "__main__":
    unittest.main()
