import json
import os
import tempfile
import unittest

from phasef.analyst import parse_analyst, parse_rerank
from phasef.analyst_mcp import Server

TASK = {"candidates": [{"round_id": "a:0", "text": "x"}, {"round_id": "b:0", "text": "y"}, {"round_id": "c:0", "text": "z"}]}


class AnalystTests(unittest.TestCase):
    def test_rerank_strict(self):
        order, omitted = parse_rerank('{"ranking": ["c:0", "a:0"]}', TASK)
        self.assertEqual((order, omitted), (["c:0", "a:0", "b:0"], 1))
        for bad in ('{"ranking": ["a:0", "zzz"]}', '{"ranking": ["a:0", "a:0"]}', '{"ranking": "a:0"}', "no json"):
            with self.assertRaises(ValueError):
                parse_rerank(bad, TASK)

    def test_analyst_only_seen_ids_max_20(self):
        ok = parse_analyst('{"results": [{"round_id": "a:0", "reason": "r"}, {"round_id": "t:1", "reason": "found by search"}]}', {"a:0", "t:1"})
        self.assertEqual([r for r, _ in ok], ["a:0", "t:1"])
        with self.assertRaises(ValueError):
            parse_analyst('{"results": [{"round_id": "q:9", "reason": "invented"}]}', {"a:0"})
        many = json.dumps({"results": [{"round_id": "a:%d" % i, "reason": ""} for i in range(21)]})
        with self.assertRaises(ValueError):
            parse_analyst(many, {"a:%d" % i for i in range(21)})

    def test_server_enforces_budget_in_code(self):
        d = tempfile.mkdtemp()
        calls = []
        srv = Server(12, os.path.join(d, "log.jsonl"), lambda req: (calls.append(req), {"results": [{"round_id": "r%d" % len(calls)}]})[1])
        texts = [srv.call_tool("search", {"query": "q"})[0] for _ in range(15)]
        self.assertEqual(len(calls), 12)  # backend reached exactly 12 times
        self.assertTrue(all("BUDGET EXHAUSTED" in t for t in texts[12:]))
        log = [json.loads(l) for l in open(os.path.join(d, "log.jsonl"))]
        self.assertEqual(sum(1 for x in log if not x["refused"]), 12)
        self.assertIn("r1", log[0]["round_ids"])

    def test_server_protocol_and_unknown_tool(self):
        srv = Server(2, os.path.join(tempfile.mkdtemp(), "l"), lambda r: {})
        self.assertEqual(len(srv.handle({"id": 1, "method": "tools/list"})["result"]["tools"]), 4)
        self.assertIn("error", srv.handle({"id": 2, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}}))
        self.assertIsNone(srv.handle({"method": "notifications/initialized"}))

    def test_backend_failure_counts_against_budget(self):
        def boom(req):
            raise RuntimeError("down")
        srv = Server(1, os.path.join(tempfile.mkdtemp(), "l"), boom)
        t, err, _ = srv.call_tool("search", {"query": "q"})
        self.assertTrue(err)
        self.assertIn("EXHAUSTED", srv.call_tool("search", {"query": "q"})[0])


if __name__ == "__main__":
    unittest.main()
