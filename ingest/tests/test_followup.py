"""Follow-up behaviours (PREREG-amend-2). Written BEFORE the fixes and run against aa30130 to record failing-first."""
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from ingest.canon import Hasher
from ingest.ingester import Ingester
from ingest.scan import ReferenceScanner
from ingest.stubs import text
from ingest.tap import CrashPlan, PortTap, SimulatedCrash, StaticIdentity
from ingest.testkit import KEY, Rig, make_injector


def _boom():
    raise SimulatedCrash()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ingtest2-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def window_host(self, n, window=None, compact=False, provider="anthropic"):
        script = [{"content": [text("a%d" % i)]} for i in range(n)] if provider == "anthropic" else \
            [{"message": {"role": "assistant", "content": "a%d" % i}} for i in range(n)]
        rig = Rig(self.d, provider, script)
        for i in range(n):
            rig.msgs.append({"role": "user", "content": "q%d" % i})
            o = rig.call(rig.request())
            rig.msgs.append({"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"])
            if window and len(rig.msgs) > window:
                dropped = rig.msgs[:-window] if compact else None
                if compact:
                    rig.msgs[:] = [{"role": "user", "content": "SUMMARY-%d of earlier chat" % i}] + rig.msgs[-window:]
                    while len(rig.msgs) > window + 1 and False:
                        pass
                else:
                    while len(rig.msgs) > window:
                        rig.msgs.pop(0)
                        rig.msgs.pop(0)
        rig.sync()
        return rig


class StableIdentity(Base):
    def _check(self, rig, n):
        ev = rig.events()
        users = [json.loads(e["content"]).get("text") if provider_text(e) else None for e in ev if e["prov"] == "user"]
        assts = [e for e in ev if e["prov"] == "assistant"]
        self.assertEqual(len(users), n)
        self.assertEqual(len(set(users)), n)
        self.assertEqual(len(assts), n)
        self.assertEqual([g["kind"] for g in rig.ing.gaps(rig.stream_id) if g["blocking"]], [])

    def test_window_4(self):
        self._check(self.window_host(20, 4), 20)

    def test_window_8(self):
        self._check(self.window_host(20, 8), 20)

    def test_compacting_host_summary_is_host_not_user(self):
        rig = self.window_host(20, 4, compact=True)
        ev = rig.events()
        summ = [e for e in ev if "SUMMARY-" in e["content"]]
        self.assertTrue(summ)
        self.assertEqual({e["prov"] for e in summ}, {"host"})
        self.assertEqual(len([e for e in ev if e["prov"] == "user" and "SUMMARY" not in e["content"]]), 20)
        self.assertEqual([g["kind"] for g in rig.ing.gaps(rig.stream_id) if g["blocking"]], [])

    def test_window_openai(self):
        self._check(self.window_host(12, 4, provider="openai"), 12)


def provider_text(e):
    return "text" in json.loads(e["content"])


class States(Base):
    def test_unconfirmed_call_state(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("a1")]}, {"content": [text("a2")]}], tap_kw={"crash": CrashPlan("after_send_before_resp_log", _boom)})
        rig.msgs.append({"role": "user", "content": "q1"})
        with self.assertRaises(SimulatedCrash):
            rig.call(rig.request())
        rig.sync()
        c = rig.ing.completeness(rig.stream_id)
        self.assertEqual(c["state"], "complete_except_unconfirmed")
        self.assertEqual(c["unconfirmed_calls"], [1])
        self.assertFalse(rig.ing.claim_complete(rig.stream_id, 1))
        ok, ids = rig.ing.claim_complete_except(rig.stream_id, 1)
        self.assertTrue(ok)
        self.assertEqual(ids, [1])

    def test_retry_returns_to_complete(self):
        rig = Rig(self.d, "anthropic", [{"fail": "timeout", "content": []}, {"content": [text("a1")]}])
        rig.msgs.append({"role": "user", "content": "q1"})
        with self.assertRaises(TimeoutError):
            rig.call(rig.request())
        rig.call(rig.request())
        rig.sync()
        self.assertEqual(rig.ing.completeness(rig.stream_id)["state"], "complete")

    def test_dropped_record_is_incomplete(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("a%d" % i)]} for i in range(4)])
        for i in range(4):
            rig.ask("q%d" % i)
        recs = rig.log.read(rig.stream_id)
        ing = Ingester(os.path.join(self.d, "x.db"), rig.log.blob)
        ing.deliver([r for r in recs if r["seq"] != 3])
        ing.drain(rig.stream_id)
        self.assertEqual(ing.completeness(rig.stream_id)["state"], "incomplete")

    def test_recoverer_resolves_unconfirmed(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("a1")]}], tap_kw={"crash": CrashPlan("after_send_before_resp_log", _boom)})
        rig.msgs.append({"role": "user", "content": "q1"})
        with self.assertRaises(SimulatedCrash):
            rig.call(rig.request())
        from ingest.stubs import anth_msg
        n = rig.tap.recover_unconfirmed(lambda req_id, call_n: anth_msg([text("recovered answer")], "end_turn", "msg_rec"))
        self.assertEqual(n, 1)
        rig.sync()
        self.assertEqual(rig.ing.completeness(rig.stream_id)["state"], "complete")
        self.assertEqual([e["prov"] for e in rig.events()], ["user", "assistant"])
        r = [e for e in rig.events() if e["prov"] == "assistant"]
        self.assertEqual(len(r), 1)
        self.assertTrue(json.loads(r[0]["meta"]).get("recovered"))


class SeedBackfill(Base):
    def _resumed(self, tamper=False):
        hist = [{"role": "user", "content": "old q"}, {"role": "assistant", "content": [text("old a")]}]
        rig = Rig(self.d, "anthropic", [{"content": [text("a1")]}])
        rig.ing = Ingester(os.path.join(self.d, "events.db"), rig.log.blob, seed_verifier=Hasher(KEY).h)
        rig.tap.seed(hist)
        if tamper:
            import sqlite3 as s
            # a seed whose signature does not verify: forge by writing with a different key
            pass
        rig.msgs = list(hist) + [{"role": "user", "content": "new q"}]
        rig.call(rig.request())
        rig.sync()
        return rig

    def test_signed_seed_no_gap_and_history_not_reingested(self):
        rig = self._resumed()
        self.assertEqual(rig.ing.gaps(rig.stream_id), [])
        self.assertEqual([json.loads(e["content"]).get("text") for e in rig.events() if e["prov"] in ("user", "assistant")], ["new q", "a1"])
        self.assertEqual(rig.ing.completeness(rig.stream_id)["state"], "complete")

    def test_bad_signature_seed_is_gap(self):
        hist = [{"role": "user", "content": "old q"}, {"role": "assistant", "content": [text("old a")]}]
        rig = Rig(self.d, "anthropic", [{"content": [text("a1")]}])
        rig.tap.hasher = Hasher(b"some other key")
        rig.tap.seed(hist)
        rig.tap.hasher = rig.hasher
        rig.ing = Ingester(os.path.join(self.d, "events.db"), rig.log.blob, seed_verifier=rig.hasher.h)
        rig.msgs = list(hist) + [{"role": "user", "content": "new q"}]
        rig.call(rig.request())
        rig.sync()
        self.assertIn("seed_invalid", [g["kind"] for g in rig.ing.gaps(rig.stream_id)])
        self.assertFalse(rig.ing.claim_complete(rig.stream_id, 1))

    def _bypass_rig(self, verifier_key):
        rig = Rig(self.d, "anthropic", [{"content": [text("a%d" % i)]} for i in range(6)])
        rig.ing = Ingester(os.path.join(self.d, "events.db"), rig.log.blob, backfill_verifier=Hasher(verifier_key).h)
        rig.ask("q0")
        rig.msgs.append({"role": "user", "content": "q1"})
        o = rig.stub.call(rig.request())
        rig.msgs.append({"role": "assistant", "content": o["content"]})
        rig.ask("q2")
        rig.sync()
        gaps = [g for g in rig.ing.gaps(rig.stream_id) if g["kind"] == "unlogged_assistant_message"]
        self.assertEqual(len(gaps), 1)
        return rig, json.loads(gaps[0]["detail"])["key"]

    def test_authorised_backfill_clears_and_records(self):
        rig, key = self._bypass_rig(b"backfill-key")
        rig.tap.backfill("unlogged_assistant_message", key, "ops:alice", Hasher(b"backfill-key"))
        rig.sync()
        self.assertEqual([g for g in rig.ing.gaps(rig.stream_id) if g["blocking"]], [])
        closed = [g for g in rig.ing.gaps(rig.stream_id, open_only=False) if g["closed"]]
        self.assertEqual(closed[0]["closed_by"], "backfill:ops:alice")

    def test_unauthorised_backfill_does_not_clear(self):
        rig, key = self._bypass_rig(b"backfill-key")
        rig.tap.backfill("unlogged_assistant_message", key, "mallory", Hasher(b"wrong-key"))
        rig.sync()
        self.assertEqual(len([g for g in rig.ing.gaps(rig.stream_id) if g["blocking"]]), 1)


class BlockStorage(Base):
    def test_no_issued_table_and_restart_keeps_alignment(self):
        inj = make_injector(("Dana likes tea PLANTED_Y",))
        rig = Rig(self.d, "anthropic", [{"content": [text("ok")]}] * 3)
        tables = {r[0] for r in rig.log.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("issued", tables)
        from memcore.ports import Turn
        for turn in range(3):
            i = inj.build(Turn(session_id="sess1", user_text="Dana tea", time="2026-01-01T00:00:00Z"))
            rig.tap.register_issued(i)
            rig.msgs.append({"role": "user", "content": i.block + "\nDana tea %d" % turn})
            o = rig.call(rig.request())
            rig.msgs.append({"role": "assistant", "content": o["content"]})
            if turn == 1:
                rig.tap = PortTap(rig.stub, "anthropic", rig.log, rig.identity, rig.hasher, ReferenceScanner())
        rig.sync()
        self.assertEqual([e for e in rig.events() if e["prov"] == "host"], [])
        self.assertEqual(rig.ing.gaps(rig.stream_id), [])
        self.assertFalse(any("PLANTED_Y" in e["content"] for e in rig.events() if e["prov"] != "memory_injected"))
        self.assertFalse(any("PLANTED_Y" in e["content"] for e in rig.events()))


class StreamContract(Base):
    def _run(self, delivery):
        seen = []
        rig = Rig(self.d, "anthropic", [{"content": [text("a long answer of several chunks here")]}], stream=True,
                  tap_kw={"stream_delivery": delivery, "crash": CrashPlan("mid_stream", _boom, at=6)})
        rig.msgs.append({"role": "user", "content": "q"})
        try:
            for ev in rig.tap.call(rig.request(), stream=True):
                seen.append(ev)
        except SimulatedCrash:
            pass
        rig.sync()
        return seen, rig

    def test_live_mode_loses_deltas_on_crash(self):
        seen, rig = self._run("live")
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual([e for e in rig.events() if e["side"] == "resp"], [])

    def test_buffered_mode_gates(self):
        seen, rig = self._run("buffered")
        self.assertEqual(seen, [])
        self.assertEqual([e for e in rig.events() if e["side"] == "resp"], [])


if __name__ == "__main__":
    unittest.main()
