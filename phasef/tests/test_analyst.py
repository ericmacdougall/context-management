import json
import os
import tempfile
import unittest

import subprocess
import sys

from phasef.analyst import (ANALYST_SYSTEM, RERANK_SYSTEM, build_analyst_prompt, build_rerank_prompt, guarded, parse_analyst, parse_rerank, qid_of)
from phasef.llm import PromptLeak
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


SECRET_QID, SECRET_LEVEL, SECRET_META = "09ba9854_abs", "H2", "META-SENTINEL-77"
LEAKTASK = {"question": "What is my cat called?", "question_date": "2023/05/30 (Tue) 12:18", "_meta": {"question_id": SECRET_QID, "level": SECRET_LEVEL, "note": SECRET_META},
            "candidates": [{"round_id": "s0123456789ab:3", "date": "2023/05/20", "session_id": "SESSION-FIELD-SENTINEL", "text": "user: my cat is Luna " + "x" * 3000}]}


class NoLeakTests(unittest.TestCase):
    def test_prompts_carry_only_question_date_candidate_lines(self):
        for prompt, system in ((build_rerank_prompt(LEAKTASK), RERANK_SYSTEM), (build_analyst_prompt(LEAKTASK), ANALYST_SYSTEM)):
            text = prompt + "\n" + system
            for forbidden in (SECRET_QID, SECRET_LEVEL, SECRET_META, "_meta", "SESSION-FIELD-SENTINEL", "own_haystack", "/Users/", "ANALYST-RERANK", "analyst_results", "_meta"):
                self.assertNotIn(forbidden, text)
            self.assertIn("What is my cat called?", text)
            self.assertIn("2023/05/30 (Tue) 12:18", text)
            self.assertIn("[s0123456789ab:3] 2023/05/20", text)
            self.assertIn("[cut]", text)  # candidate text capped at 1,200 characters
            self.assertNotIn("x" * 1300, text)

    def test_question_id_and_level_read_from_meta_or_top_level(self):
        self.assertEqual(qid_of(LEAKTASK), (SECRET_QID, SECRET_LEVEL))
        self.assertEqual(qid_of({"question_id": "a", "level": "H0"}), ("a", "H0"))

    def test_send_time_guard_refuses_question_id_level_and_level_names(self):
        ok = build_rerank_prompt(LEAKTASK)
        guarded(LEAKTASK, ok, RERANK_SYSTEM)
        for bad in (ok + " " + SECRET_QID, ok + " H2", ok + " own_haystack", ok + " H0", ok + " H3"):
            with self.assertRaises(PromptLeak):
                guarded(LEAKTASK, bad, RERANK_SYSTEM)

    def test_mcp_server_never_echoes_ids_in_results_or_errors(self):
        d = tempfile.mkdtemp()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analyst_mcp.py")
        log = os.path.join(d, SECRET_QID + "_" + SECRET_LEVEL + ".log")  # secret-bearing path handed to the server
        task = os.path.join(d, "t.json")
        json.dump({"haystack": [{"round_id": "s1:0", "session_id": "s1", "date": "2023/01/01", "text": "hello cat"}]}, open(task, "w"))
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "cat"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_round", "arguments": {"round_id": "nope"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "timeline", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "bogus", "arguments": {}}}]
        out = subprocess.run([sys.executable, script, "--budget", "3", "--log", log, "--fake", task], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True).stdout
        for forbidden in (SECRET_QID, SECRET_LEVEL, "t.json", d):
            self.assertNotIn(forbidden, out)
        timeline = json.loads(json.loads(out.splitlines()[4])["result"]["content"][0]["text"])
        self.assertIn("more", timeline)
        self.assertNotIn("total", timeline)


if __name__ == "__main__":
    unittest.main()
