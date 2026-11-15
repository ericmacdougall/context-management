"""A stub OpenAI-compatible chat backend that can EMIT TOOL CALLS (the gateway only supports one forced function; Agent Builder
sends tools with tool_choice auto). Deterministic rules, records every request body (secrets excluded) for fixtures.
Rules: a tool message in the history -> final text mentioning the tool result; tools present and the last user text contains
'staging' -> call the tool whose name contains 'query_facts' (else the first tool) with {"subject": "staging database"};
otherwise echo text. Serves stream true and false. Bearer token required. For development tests only."""
from __future__ import annotations

import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from ..sse import chat_chunks


def decide(body: dict) -> dict:
    msgs = body.get("messages") or []
    text_of = lambda m: m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    last_user = next((text_of(m) for m in reversed(msgs) if m.get("role") == "user"), "")
    tools = body.get("tools") or []
    if tool_msgs:
        return {"text": "Per the tool the staging database is in Dublin. (tool said: %s)" % text_of(tool_msgs[-1])[:120]}
    if tools and "staging" in last_user.lower():
        pick = next((t for t in tools if "query_facts" in t["function"]["name"]), tools[0])
        return {"tool": {"id": "call_stub1", "name": pick["function"]["name"], "arguments": json.dumps({"subject": "staging database"})}}
    return {"text": "Stub answer: " + last_user[:100]}


class ToolStub:
    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0, delay_s: float = 0.0):
        self.requests: List[dict] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a): pass

            def do_POST(self):
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                    return self._j(401, {"error": {"message": "bad token"}})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "body": body})
                if delay_s:
                    time.sleep(delay_s)
                d = decide(body)
                text = d.get("text", "")
                tool = d.get("tool")
                usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                model = body.get("model") or "stub-tool"
                if body.get("stream"):
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
                    for c in chat_chunks("chatcmpl-stub", model, int(time.time()), text, usage, tool_call=tool):
                        self.wfile.write(c); self.wfile.flush()
                    self.close_connection = True
                else:
                    msg = {"role": "assistant", "content": text}
                    if tool:
                        msg["tool_calls"] = [{"id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": tool["arguments"]}}]
                    self._j(200, {"id": "chatcmpl-stub", "object": "chat.completion", "created": int(time.time()), "model": model,
                                  "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}], "usage": usage})

            def _j(self, code, o):
                raw = json.dumps(o).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        import ipaddress
        ip = ipaddress.ip_address(host)
        if not (ip.is_loopback or (ip.is_private and host == "172.17.0.1")):
            raise ValueError("stub binds loopback or the Docker bridge address only")
        import threading
        self.srv = ThreadingHTTPServer((host, port), H)
        self.srv.daemon_threads = True
        self.host, self.port = self.srv.server_address[:2]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()
