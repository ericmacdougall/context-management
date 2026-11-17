import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.request

from memcore.mcp_http import McpHttpServer, make_token_file, read_token
from memcore.mcp_server import demo_store
from memcore.memtools import memory_tools


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        cls.tf = os.path.join(d, "tok")
        cls.tok = make_token_file(cls.tf)
        cls.srv = McpHttpServer(memory_tools(demo_store()), cls.tok).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def post(self, msg, token=None, origin=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not False:
            h["Authorization"] = "Bearer " + (token or self.tok)
        if origin:
            h["Origin"] = origin
        return urllib.request.urlopen(urllib.request.Request(self.srv.url, data=json.dumps(msg).encode(), headers=h, method="POST"))

    def test_token_file_mode_and_loopback(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.tf).st_mode), 0o600)
        self.assertEqual(self.srv.host, "127.0.0.1")
        os.chmod(self.tf, 0o644)
        with self.assertRaises(PermissionError):
            read_token(self.tf)
        os.chmod(self.tf, 0o600)
        for bad in ("0.0.0.0", "8.8.8.8"):
            with self.assertRaises(ValueError):
                McpHttpServer(memory_tools(demo_store()), "t", bad)

    def test_list_and_call_with_text_and_structured_content(self):
        r = json.load(self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        self.assertEqual(len(r["result"]["tools"]), 4)
        r = json.load(self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query_facts", "arguments": {"subject": "staging database"}}}))["result"]
        self.assertIn("Dublin", r["content"][0]["text"])                       # full result as text
        self.assertEqual(len(r["structuredContent"]["result"]), 2)             # and as an object

    def test_notification_gets_202_no_body(self):
        r = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((r.status, r.read()), (202, b""))

    def test_auth_origin_and_get(self):
        for kw, code in (({"token": "wrong"}, 401), ({"token": False}, 401), ({"origin": "http://evil.example"}, 403)):
            with self.assertRaises(urllib.error.HTTPError) as c:
                self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, **kw)
            self.assertEqual(c.exception.code, code)
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(urllib.request.Request(self.srv.url, headers={"Authorization": "Bearer " + self.tok}))
        self.assertEqual(c.exception.code, 405)
        self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="http://localhost:3000")  # local origin ok


if __name__ == "__main__":
    unittest.main()
