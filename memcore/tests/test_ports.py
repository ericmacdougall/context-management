import io
import json
import unittest

from memcore.fakes import HashEmbedder, InMemorySearch, OverlapReranker
from memcore.fusion import linear, rrf
from memcore.jsonschema_min import validate
from memcore.mcp_server import demo_store, handle, serve
from memcore.memtools import BudgetedInjector, memory_tools
from memcore.ports import Embedder, Turn


class Recording(HashEmbedder):
    def _embed(self, texts, kind):
        self.seen = list(texts)
        return super()._embed(texts, kind)


def check_prefix_contract(e):
    e.embed(["hello"], "query")
    assert e.seen == ["query: hello"], e.seen
    e.embed(["hello"], "passage")
    assert e.seen == ["passage: hello"], e.seen


class BrokenNoPrefix(Recording):
    query_prefix = ""
    passage_prefix = ""


class EmbedderTests(unittest.TestCase):
    def test_prefixes_applied_by_port(self):
        check_prefix_contract(Recording())

    def test_broken_variant_fails(self):
        with self.assertRaises(AssertionError):
            check_prefix_contract(BrokenNoPrefix())

    def test_kind_validated_and_str_rejected(self):
        with self.assertRaises(ValueError):
            Recording().embed(["a"], "doc")
        with self.assertRaises(TypeError):
            Recording().embed("a", "query")

    def test_query_passage_differ(self):
        e = HashEmbedder()
        self.assertNotEqual(e.embed(["x y"], "query").vectors, e.embed(["x y"], "passage").vectors)
        self.assertEqual(e.embed(["x y"], "query").dimensions, 64)


class SchemaTests(unittest.TestCase):
    S = {"type": "object", "properties": {"n": {"type": "integer"}, "t": {"type": "array", "items": {"type": "string"}}},
         "required": ["n", "t"], "additionalProperties": False}

    def test_valid_and_invalid(self):
        self.assertEqual(validate({"n": 1, "t": ["a"]}, self.S), [])
        self.assertTrue(validate({"n": True, "t": []}, self.S))      # bool is not an integer
        self.assertTrue(validate({"n": 1}, self.S))                  # missing required
        self.assertTrue(validate({"n": 1, "t": [], "x": 1}, self.S)) # extra property
        self.assertTrue(validate({"n": 1, "t": [3]}, self.S))


class FusionTests(unittest.TestCase):
    def test_rrf_prefers_agreement(self):
        r = rrf([["a", "b", "c"], ["c", "b", "d"]])
        self.assertEqual([d for d, _ in r][:2], ["c", "b"])

    def test_rrf_dedup_and_determinism(self):
        self.assertEqual(rrf([["a", "a"], ["a"]]), rrf([["a", "a"], ["a"]]))
        self.assertAlmostEqual(rrf([["a", "a"]])[0][1], 1 / 61)

    def test_broken_rrf_would_fail_dedup(self):
        # a naive implementation double counts duplicates; assert our value differs from it
        naive = 1 / 61 + 1 / 62
        self.assertNotAlmostEqual(rrf([["a", "a"]])[0][1], naive)

    def test_linear_constant_scores(self):
        self.assertEqual(linear([{"a": 5.0, "b": 5.0}]), [("a", 1.0), ("b", 1.0)])

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            rrf([["a"]], weights=[1, 2])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.e, self.s = HashEmbedder(), InMemorySearch()
        for i, (t, tenant) in enumerate([("staging database in Dublin", "a"), ("lunch menu on friday", "a"),
                                         ("database backup schedule", "b")]):
            self.s.add("idx", "d%d" % i, {"text": t, "tenant": tenant, "vector": self.e.embed([t], "passage").vectors[0]})

    def test_filter_and_fusion_location(self):
        q = "database"
        r = self.s.fused("idx", q, self.e.embed([q], "query").vectors[0], {"tenant": "a"}, 5)
        self.assertEqual(r.fusion, "client")
        self.assertNotIn("d2", [h.id for h in r.hits])
        self.assertEqual(r.hits[0].id, "d0")
        self.assertEqual(self.s.lexical("idx", q).fusion, "none")

    def test_rerank(self):
        h = OverlapReranker().rerank("staging database", ["lunch", "the staging database", "database"], 2)
        self.assertEqual([x.index for x in h], [1, 2])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.surf = memory_tools(demo_store())

    def test_four_tools_described_as_data(self):
        names = [t["name"] for t in self.surf.list_tools()]
        self.assertEqual(names, ["recall", "open_event", "query_facts", "timeline"])
        json.dumps(self.surf.list_tools())  # serialisable

    def test_query_facts_as_of(self):
        old = self.surf.call_tool("query_facts", {"subject": "staging database", "as_of": "2026-02-01T00:00:00Z"})
        new = self.surf.call_tool("query_facts", {"subject": "staging database"})
        self.assertEqual([f["value"] for f in old], ["Frankfurt"])
        self.assertEqual([f["value"] for f in new], ["Frankfurt", "Dublin"])
        cur = self.surf.call_tool("query_facts", {"subject": "staging database", "as_of": "2026-04-01T00:00:00Z"})
        self.assertEqual([f["value"] for f in cur], ["Dublin"])

    def test_recall_as_of_hides_future(self):
        r = self.surf.call_tool("recall", {"query": "staging database", "as_of": "2026-02-01T00:00:00Z"})
        self.assertEqual([x["id"] for x in r], ["ev1"])

    def test_invalid_args_rejected(self):
        with self.assertRaises(ValueError):
            self.surf.call_tool("recall", {})
        with self.assertRaises(ValueError):
            self.surf.call_tool("recall", {"query": "x", "bogus": 1})
        with self.assertRaises(KeyError):
            self.surf.call_tool("nope", {})

    def test_injector_budget_and_ids(self):
        inj = BudgetedInjector(demo_store(), char_budget=420)    # the budget is the RENDERED size (header and END line included)
        out = inj.build(Turn("s", "where is the staging database", "2026-12-01T00:00:00Z"))
        self.assertEqual(len(out.used_ids), 1)
        self.assertLessEqual(len(out.block), 420)
        self.assertEqual(out.dropped, 1)
        self.assertIn('"id":"%s"' % out.used_ids[0], out.block)
        self.assertEqual(BudgetedInjector(demo_store()).build(Turn("s", "zzz", "2026-12-01T00:00:00Z")).block, "")


class McpTests(unittest.TestCase):
    def rpc(self, *msgs):
        fin = io.StringIO("".join(json.dumps(m) + "\n" for m in msgs))
        fout = io.StringIO()
        serve(memory_tools(demo_store()), fin, fout)
        return [json.loads(l) for l in fout.getvalue().splitlines()]

    def test_handshake_list_call(self):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                       {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "open_event", "arguments": {"id": "ev2"}}},
                       {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "recall", "arguments": {}}},
                       {"jsonrpc": "2.0", "id": 5, "method": "bogus"})
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4, 5])  # notification got no reply
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(out[1]["result"]["tools"]), 4)
        self.assertIn("Dublin", out[2]["result"]["content"][0]["text"])
        self.assertTrue(out[3]["result"]["isError"])
        self.assertEqual(out[4]["error"]["code"], -32601)

    def test_broken_variant_reply_to_notification_detected(self):
        # JSON-RPC forbids replying to a notification; handle() must return None for it
        self.assertIsNone(handle(memory_tools(demo_store()), {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNotNone(handle(memory_tools(demo_store()), {"jsonrpc": "2.0", "id": 0, "method": "ping"}))


if __name__ == "__main__":
    unittest.main()
