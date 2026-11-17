"""Replay of what Kibana 9.5.6's MCP connector actually sent (recorded on t2-dev through the memcore MCP HTTP server), no Kibana needed."""
import json
import os
import unittest

from memcore.mcp_server import demo_store, handle
from memcore.memtools import memory_tools

FX = os.path.join(os.path.dirname(__file__), "fixtures", "kibana956")


def load(n):
    with open(os.path.join(FX, n)) as fh:
        return json.load(fh)


class KibanaReplay(unittest.TestCase):
    def test_recorded_requests_get_the_recorded_answers(self):
        surf = memory_tools(demo_store())
        for rec in load("exchange.json")["mcp"]:
            got = handle(surf, rec["mcp_request"])
            self.assertEqual(got is None, rec["mcp_response"] is None)
            if got:
                self.assertEqual(got["result"]["content"] if "content" in got["result"] else got["result"].get("tools", got["result"]).__len__(),
                                 rec["mcp_response"]["result"]["content"] if "content" in rec["mcp_response"]["result"] else rec["mcp_response"]["result"].get("tools", rec["mcp_response"]["result"]).__len__())

    def test_kibana_client_facts(self):
        ex = load("exchange.json")["mcp"]
        init = ex[0]["mcp_request"]
        self.assertEqual(init["params"]["protocolVersion"], "2025-11-25")
        self.assertTrue(init["params"]["clientInfo"]["name"].startswith("kibana-mcp-connector-"))
        self.assertEqual([m["mcp_request"]["method"].split("/")[0] for m in ex].count("initialize"), 2)   # a fresh handshake per operation

    def test_kibana_keeps_text_content_only(self):
        res = load("converse_ok.json")["steps"][0]["results"][0]["data"]
        self.assertEqual(list(res), ["content"])                                                       # structuredContent is not passed on
        self.assertIn("Dublin", res["content"][0]["text"])

    def test_workflow_semantics_observed_on_956(self):
        w = load("workflow_direct.json")
        self.assertEqual(w["ok"]["status"], "completed")
        self.assertEqual(w["fail"]["status"], "failed")
        self.assertIn("ECONNREFUSED", w["fail"]["error"]["message"])
        slow = load("workflow_slow.json")
        self.assertEqual([r["status"] for r in slow], ["waiting", "waiting", "waiting", "completed"])  # still running at 125 s, done at 150 s


if __name__ == "__main__":
    unittest.main()
