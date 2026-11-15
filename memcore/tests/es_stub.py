"""A stub of Elasticsearch 9.5's POST /_inference/chat_completion/{id}/_stream, built from the research (not observed yet).
Strict request parser (unknown field -> 400), SSE with a BOM before every event, bare or wrapped chunks, final usage chunk,
[DONE], optional 'event: error'. Backed by a deterministic rule-based 'model' so the ChatModel contract suite can run without
any real model."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED = {"messages", "model", "max_completion_tokens", "stop", "temperature", "tools", "tool_choice", "top_p"}
BOM = "﻿"


def rule_model(system, user):
    if system and "PINEAPPLE" in system:
        return "PINEAPPLE"
    if "lighthouse" in user:
        time.sleep(5)
        return "Once upon a time."
    if "capital of France" in user:
        return "Paris"
    return "ok"


def rule_extract(user):
    m = re.search(r"Text: (\w+) travelled to (\w+) and paid (\d+) dollars for a (\w+)", user)
    return {"person": m.group(1), "city": m.group(2), "amount": int(m.group(3)), "tags": [m.group(4)]} if m else {}


class Stub:
    def __init__(self, wrapped=False, bom=True, error_after=None, bad_usage=False):
        self.wrapped, self.bom, self.error_after, self.requests = wrapped, bom, error_after, []
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if not self.path.startswith("/_inference/chat_completion/") or not self.path.endswith("/_stream"):
                    return self._j(404, {"error": "no handler"})
                bad = set(body) - ALLOWED
                if bad:
                    return self._j(400, {"error": {"type": "x_content_parse_exception", "reason": "unknown field %s" % sorted(bad)}})
                if self.headers.get("Authorization") != "ApiKey test":
                    return self._j(401, {"error": "unauthorized"})
                msgs = body["messages"]
                system = next((m["content"] for m in msgs if m["role"] == "system"), None)
                user = [m for m in msgs if m["role"] == "user"][-1]["content"]
                if len(user) > 100000:
                    return self._j(400, {"error": {"reason": "maximum context length exceeded"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                tool = (body.get("tool_choice") or {}).get("function", {}).get("name")
                if tool:
                    out = json.dumps(rule_extract(user))
                    deltas = [{"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": tool, "arguments": out[:10]}}]},
                              {"tool_calls": [{"index": 0, "function": {"arguments": out[10:]}}]}]
                else:
                    out = rule_model(system, user)
                    deltas = [{"content": out[:2]}, {"content": out[2:]}]
                events = [{"id": "r1", "object": "chat.completion.chunk", "model": "stub", "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
                events.append({"id": "r1", "object": "chat.completion.chunk", "model": "stub", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                events.append({"id": "r1", "object": "chat.completion.chunk", "model": "stub", "choices": [],
                               "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(out) // 4, "total_tokens": (len(user) + len(out)) // 4}})
                for i, e in enumerate(events):
                    if stub.error_after is not None and i == stub.error_after:
                        self._w("event: error\ndata: %s\n\n" % json.dumps({"error": {"message": "boom"}}))
                        return
                    self._w("event: message\ndata: %s\n\n" % json.dumps({"chat_completion": e} if stub.wrapped else e))
                self._w("event: message\ndata: [DONE]\n\n")

            def _w(self, s):
                self.wfile.write(((BOM if stub.bom else "") + s).encode("utf-8"))
                self.wfile.flush()

            def _j(self, code, o):
                raw = json.dumps(o).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
