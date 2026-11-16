import json
import os
import shutil
import tempfile
import unittest

from memcore.guard import EgressGuard, EgressLeak
from ingest import shapes
from ingest.ingester import Ingester
from ingest.scan import SecretBlocked
from ingest.stubs import text, thinking, tool_use, tool_result
from ingest.testkit import Rig, make_injector


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ingtest-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)


class ShapeTests(Base):
    def test_anthropic_blocks_and_provenance(self):
        msgs = [{"role": "user", "content": [text("hi"), {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]},
                {"role": "assistant", "content": [thinking("t", "SIG"), {"type": "redacted_thinking", "data": "ENC"}, text("a"),
                                                  tool_use("t1", "x", {}), {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
                                                  {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []}, {"type": "future_block"}]},
                {"role": "user", "content": [tool_result("t1", "r")]}]
        got = [[(b.prov, b.kind) for b in shapes.blocks_of_message("anthropic", m)] for m in msgs]
        self.assertEqual(got[0], [("user", "text"), ("user", "media")])
        self.assertEqual(got[1], [("assistant_thinking", "thinking"), ("assistant_thinking", "redacted_thinking"), ("assistant", "text"),
                                  ("assistant", "tool_call"), ("assistant", "server_tool_call"), ("server_tool", "web_search_tool_result"),
                                  ("unknown", "future_block")])
        self.assertEqual(got[2], [("tool", "tool_result")])

    def test_openai_blocks(self):
        m = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "ok"}, {"role": "developer", "content": "be brief"}]
        self.assertEqual([(b.prov, b.kind, b.tool_id) for b in shapes.blocks_of_message("openai", m[0])], [("assistant", "tool_call", "c1")])
        self.assertEqual([(b.prov, b.kind, b.tool_id) for b in shapes.blocks_of_message("openai", m[1])], [("tool", "tool_result", "c1")])
        self.assertEqual(shapes.blocks_of_message("openai", m[2])[0].prov, "system")
        self.assertEqual(shapes.check_pairing("openai", m[:2]), [])
        self.assertTrue(shapes.check_pairing("openai", [m[0], {"role": "user", "content": "x"}]))

    def test_pairing_violations_anthropic(self):
        bad = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [tool_use("t1", "x", {})]},
               {"role": "user", "content": [text("oops"), tool_result("t1", "r")]}]
        self.assertTrue(any("after text" in v for v in shapes.check_pairing("anthropic", bad)))
        self.assertTrue(shapes.check_pairing("anthropic", [{"role": "user", "content": "q"}, {"role": "assistant", "content": [tool_use("t1", "x", {})]},
                                                           {"role": "user", "content": "no result"}]))

    def test_max_tokens_stream_is_complete_stream_not_complete_turn(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("cut off")], "stop": "max_tokens"}], stream=True)
        rig.ask("go")
        rig.sync()
        self.assertEqual({e["status"] for e in rig.events() if e["side"] == "resp"}, {"turn_incomplete"})

    def test_signature_verbatim_and_thinking_not_assistant(self):
        sig = "Eqd/+=éweird=="
        rig = Rig(self.d, "anthropic", [{"content": [thinking("secret reasoning", sig), text("ok")]}], stream=True)
        rig.ask("q")
        rig.sync()
        th = [e for e in rig.events() if e["kind"] == "thinking"][0]
        self.assertEqual(json.loads(th["content"])["signature"], sig)
        self.assertEqual(th["prov"], "assistant_thinking")


class IngestTests(Base):
    def test_retry_after_timeout_ingests_once(self):
        rig = Rig(self.d, "anthropic", [{"fail": "timeout", "content": []}, {"content": [text("a1")]}])
        rig.msgs.append({"role": "user", "content": "hello"})
        with self.assertRaises(TimeoutError):
            rig.call(rig.request())
        o = rig.call(rig.request())
        rig.sync()
        ev = rig.events()
        self.assertEqual([(e["prov"], json.loads(e["content"])["text"]) for e in ev], [("user", "hello"), ("assistant", "a1")])
        self.assertTrue(any(e["kind"] == "call_failed_unknown" for e in rig.events(include_port=True)))

    def test_identical_resend_is_marked_retry(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("a1")]}, {"content": [text("a1b")]}])
        rig.msgs.append({"role": "user", "content": "hello"})
        rig.call(rig.request())
        rig.call(rig.request())                       # host never saw the first answer and resends the same request
        rig.sync()
        self.assertEqual(len([e for e in rig.events() if e["prov"] == "user"]), 1)
        self.assertEqual(len([e for e in rig.events() if e["prov"] == "assistant"]), 2)
        resp = [e for e in rig.events() if e["side"] == "resp"]
        self.assertEqual([json.loads(e["meta"]).get("retry_of") for e in resp], [None, 1])     # the second answer carries the retry marker

    def test_openai_stream_tool_calls(self):
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": json.dumps({"a": 1})}}]
        rig = Rig(self.d, "openai", [{"message": {"role": "assistant", "content": None, "tool_calls": tc}, "stop": "tool_calls"},
                                     {"message": {"role": "assistant", "content": "done"}}], stream=True)
        rig.ask("go")
        rig.msgs.append({"role": "tool", "tool_call_id": "c1", "content": "res"})
        o = rig.call(rig.request())
        rig.sync()
        kinds = [(e["prov"], e["kind"]) for e in rig.events()]
        self.assertEqual(kinds, [("user", "text"), ("assistant", "tool_call"), ("tool", "tool_result"), ("assistant", "text")])
        self.assertEqual(rig.ing.completeness(rig.stream_id)["blocking_gaps"], [])

    def test_gap_blocks_claim_and_late_record_closes(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("a%d" % i)]} for i in range(6)])
        for i in range(6):
            rig.ask("q%d" % i)
        recs = rig.log.read(rig.stream_id)
        ing = Ingester(os.path.join(self.d, "x.db"), rig.log.blob)
        ing.deliver([r for r in recs if r["seq"] != 5])
        ing.drain(rig.stream_id)
        self.assertFalse(ing.claim_complete(rig.stream_id, len(recs)))
        self.assertEqual([g["kind"] for g in ing.gaps(rig.stream_id)], ["seq_hole"])
        ing.deliver([r for r in recs if r["seq"] == 5])
        ing.drain(rig.stream_id)
        self.assertTrue(ing.claim_complete(rig.stream_id, len(recs)))

    def test_egress_guard_blocks_and_tombstones(self):
        g = EgressGuard(real_ids=["conv-7f3a"])
        rig = Rig(self.d, "anthropic", [{"content": [text("ok")]}], tap_kw={"guard": g})
        rig.msgs.append({"role": "user", "content": "please look at conv-7f3a now"})
        with self.assertRaises(EgressLeak):
            rig.call(rig.request())
        self.assertEqual(rig.stub.received, [])
        rig.msgs[:] = [{"role": "user", "content": "clean question"}]
        rig.call(rig.request())
        rig.sync()
        self.assertEqual(rig.log.head(rig.stream_id)[0], 3)           # BLOCK tombstone kept the sequence contiguous
        self.assertTrue(rig.ing.claim_complete(rig.stream_id, 3))
        self.assertNotIn("conv-7f3a", json.dumps([e["content"] for e in rig.events()]))

    def test_secret_policy_block(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("ok")]}], tap_kw={"secret_policy": "block"})
        rig.msgs.append({"role": "user", "content": "key sk-live-abcdefghijklmnopqrstuvwx"})
        with self.assertRaises(SecretBlocked):
            rig.call(rig.request())
        self.assertEqual(rig.stub.received, [])

    def test_injected_never_user(self):
        inj = make_injector(("Dana likes tea PLANTED_X",))
        rig = Rig(self.d, "anthropic", [{"content": [text("ok")]}] * 2, injector=inj)
        rig.ask("what does Dana like with tea")
        rig.sync()
        self.assertTrue(any("PLANTED_X" in cj for r in rig.stub.received for cj in [json.dumps(r)]))
        self.assertFalse(any("PLANTED_X" in e["content"] for e in rig.events() if e["prov"] != "memory_injected"))
        self.assertEqual([e["prov"] for e in rig.events()].count("memory_injected"), 1)

    def test_injection_after_tool_result_keeps_provider_valid_order(self):
        inj = make_injector(("Dana likes tea",))
        rig = Rig(self.d, "anthropic", [{"content": [tool_use("t1", "x", {})], "stop": "tool_use"}, {"content": [text("ok")]}], injector=inj)
        rig.ask("Dana tea please")
        rig.msgs.append({"role": "user", "content": [tool_result("t1", "res"), text("Dana tea continue")]})
        rig.call(rig.request())
        sent = rig.stub.received[-1]
        self.assertEqual(shapes.check_pairing("anthropic", sent["messages"]), [])
        last = sent["messages"][-1]["content"]
        self.assertEqual([b["type"] for b in last][:2], ["tool_result", "text"])
        self.assertIn("MEMCORE", last[1]["text"])

    def test_openai_null_fields_do_not_change_message_key(self):
        a = {"role": "assistant", "content": "x", "refusal": None, "tool_calls": []}
        self.assertEqual(shapes.message_key("openai", a), shapes.message_key("openai", {"role": "assistant", "content": "x"}))

    def test_log_is_append_only(self):
        rig = Rig(self.d, "anthropic", [{"content": [text("ok")]}])
        rig.ask("q")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            rig.log.conn().execute("UPDATE port_log SET body='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            rig.log.conn().execute("DELETE FROM port_log")


if __name__ == "__main__":
    unittest.main()
