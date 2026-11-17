import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memcore.adapters.anthropic_api import AnthropicMessagesChat
from memcore.adapters.elastic_inference import ElasticInferenceChat
from memcore.tests.es_stub import Stub

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}


class Api:
    def __init__(self):
        self.reqs = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.reqs.append((self.path, {k.lower(): v for k, v in self.headers.items()}, b))
                content = [{"type": "tool_use", "id": "t", "name": "answer", "input": {"a": 5}}] if b.get("tools") else [{"type": "text", "text": "hi"}]
                raw = json.dumps({"content": content, "model": b["model"], "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 10}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class AnthropicStubTests(unittest.TestCase):
    def mk(self, api):
        d = tempfile.mkdtemp()
        return AnthropicMessagesChat("claude-test", lambda: "STUB-KEY-NOT-REAL", base_url=api.url, cache_dir=d, ledger_path=os.path.join(d, "l"), cap=9)

    def test_shape_headers_usage_and_forced_tool(self):
        api = Api()
        m = self.mk(api)
        r = m.complete([{"role": "user", "content": "x"}], system="S")
        path, hdr, body = api.reqs[0]
        self.assertEqual((path, hdr["anthropic-version"], hdr["x-api-key"]), ("/v1/messages", "2023-06-01", "STUB-KEY-NOT-REAL"))
        self.assertEqual((body["system"], body["max_tokens"] > 0, r.text), ("S", True, "hi"))
        self.assertEqual((r.usage.input_tokens, r.usage.cached_input_tokens, r.usage.output_tokens), (13, 10, 4))
        r2 = m.complete([{"role": "user", "content": "y"}], json_schema=SCHEMA)
        self.assertEqual((r2.parsed, api.reqs[1][2]["tool_choice"]), ({"a": 5}, {"type": "tool", "name": "answer"}))
        self.assertNotIn("STUB-KEY", open(m.ledger_path).read())
        api.srv.shutdown()

    def test_key_is_never_read_from_environment(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        api = Api()
        self.assertEqual(self.mk(api).complete([{"role": "user", "content": "z"}]).text, "hi")  # uses only the callable
        api.srv.shutdown()

    def test_elastic_inference_adapter_with_other_service_endpoint_same_wire(self):
        s = Stub()
        try:
            d = tempfile.mkdtemp()
            m = ElasticInferenceChat(s.url, "mc-anthropic-chat", lambda: "ApiKey test", cache_dir=d, ledger_path=os.devnull, cap=5)
            m.complete([{"role": "user", "content": "What is the capital of France?"}])
            self.assertEqual(s.requests[0]["path"], "/_inference/chat_completion/mc-anthropic-chat/_stream")  # service is the endpoint's business
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
