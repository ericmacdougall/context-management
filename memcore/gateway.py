"""Local OpenAI-compatible gateway in front of any ChatModel adapter (claude-cli, codex-cli, ...).

SINGLE-USER, LOCAL, DEVELOPMENT ONLY. Binds 127.0.0.1 and refuses anything else; a random bearer token is written to a
0600 file; the backend adapter's call cap, cache and ledger apply to every request. Streaming is SIMULATED: the backend
call completes first, then the text is framed as SSE chunks with a final usage chunk.
Start:  python3 -m memcore.gateway --backend claude-cli --out <dir>      (writes <dir>/gateway.{token,port,pid})
Stop:   touch <dir>/gateway.stop     (the server polls for it and shuts down cleanly)
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .ports import (BackendError, CallCapExceeded, CallTimeout, ChatModel, InputTooLong, PortError, SchemaError)
from .sse import chat_chunks

MAX_BODY = 8 * 1024 * 1024
BANNER = ("=" * 72 + "\nmemcore gateway: SINGLE-USER, LOCAL, DEVELOPMENT ONLY. Bound to %s:%d.\n"
          "Backed by a subscription CLI; every request spends the backend call cap. Do not expose.\n" + "=" * 72)


def require_loopback(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False  # hostnames (even 'localhost') are refused: no resolver surprises
    if not ok:
        raise ValueError("refusing non-loopback bind %r; only 127.0.0.1 / ::1 are allowed" % host)


def to_port_request(body: dict) -> dict:
    """OpenAI chat request -> ChatModel.complete kwargs. Raises ValueError for what we do not support."""
    msgs, system = [], []
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):  # content parts: text only
            if any(p.get("type") != "text" for p in content):
                raise ValueError("only text content parts are supported")
            content = "".join(p.get("text", "") for p in content)
        if not isinstance(content, str):
            raise ValueError("message content must be a string")
        if role in ("system", "developer"):
            system.append(content)
        elif role in ("user", "assistant"):
            msgs.append({"role": role, "content": content})
        else:
            raise ValueError("unsupported message role %r (tools are not supported)" % (role,))
    if not msgs:
        raise ValueError("no user message")
    if body.get("tools") or body.get("functions"):
        raise ValueError("tools are not supported by this gateway yet")
    if (body.get("n") or 1) != 1:
        raise ValueError("n must be 1")
    schema = None
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise ValueError("response_format.json_schema.schema missing")
    elif rf.get("type") == "json_object":
        schema = {"type": "object"}
    return {"messages": msgs, "system": "\n\n".join(system) if system else None, "json_schema": schema,
            "max_output_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
            "temperature": body.get("temperature")}


def make_handler(backend: ChatModel, token: str):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # no request logging: bodies are private
            pass

        def _send(self, code, obj, extra=None):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _err(self, code, msg, ecode=None):
            self._send(code, {"error": {"message": msg, "type": "invalid_request_error" if code < 500 else "server_error", "code": ecode}})

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                return True
            self._err(401, "invalid or missing bearer token", "invalid_api_key")
            return False

        def do_GET(self):
            if not self._auth():
                return
            if self.path.rstrip("/") == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": backend.model, "object": "model", "created": 0, "owned_by": "memcore-gateway"}]})
            else:
                self._err(404, "not found")

        def do_POST(self):
            if not self._auth():
                return
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._err(404, "not found")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._err(413 if n > MAX_BODY else 400, "bad body length")
            try:
                body = json.loads(self.rfile.read(n))
                kw = to_port_request(body)
            except (ValueError, AttributeError) as e:
                return self._err(400, str(e))
            try:
                res = backend.complete(**kw)
            except InputTooLong as e:
                return self._err(400, str(e), "context_length_exceeded")
            except CallCapExceeded as e:
                return self._err(429, str(e), "call_cap_exceeded")
            except CallTimeout as e:
                return self._err(504, str(e))
            except SchemaError as e:
                return self._err(502, str(e), "schema_validation_failed")
            except (BackendError, PortError) as e:
                return self._err(502, str(e))
            usage = None
            if res.usage is not None:
                usage = {"prompt_tokens": res.usage.input_tokens, "completion_tokens": res.usage.output_tokens,
                         "total_tokens": (res.usage.input_tokens or 0) + (res.usage.output_tokens or 0),
                         "prompt_tokens_details": {"cached_tokens": res.usage.cached_input_tokens}}
            cid, created = "chatcmpl-" + res.request_hash[:24], int(time.time())
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in chat_chunks(cid, backend.model, created, res.text, usage if (body.get("stream_options") or {}).get("include_usage") else None):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
            else:
                self._send(200, {"id": cid, "object": "chat.completion", "created": created, "model": backend.model,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": res.text}, "finish_reason": "stop"}],
                                 **({"usage": usage} if usage else {}), "x_memcore": {"cached": res.cached, "request_hash": res.request_hash}})
    return H


class Gateway:
    def __init__(self, backend: ChatModel, out_dir: str, host: str = "127.0.0.1", port: int = 0):
        require_loopback(host)
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir, self.token = out_dir, secrets.token_urlsafe(32)
        self.srv = ThreadingHTTPServer((host, port), make_handler(backend, self.token))
        self.srv.daemon_threads = True
        self.host, self.port = self.srv.server_address[:2]
        self._write("gateway.token", self.token, 0o600)
        self._write("gateway.port", str(self.port), 0o600)
        self._write("gateway.pid", str(os.getpid()), 0o600)
        self.stop_file = os.path.join(out_dir, "gateway.stop")
        if os.path.exists(self.stop_file):
            os.unlink(self.stop_file)

    def _write(self, name, text, mode):
        p = os.path.join(self.out_dir, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)

    @property
    def base_url(self):
        return "http://%s:%d/v1" % (self.host, self.port)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        threading.Thread(target=self._watch_stop, daemon=True).start()
        return t

    def _watch_stop(self):
        while not os.path.exists(self.stop_file):
            time.sleep(0.5)
        self.shutdown()

    def shutdown(self):
        self.srv.shutdown()
        self.srv.server_close()
        try:
            os.unlink(os.path.join(self.out_dir, "gateway.token"))  # a stale token must not outlive the server
        except FileNotFoundError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["claude-cli", "codex-cli"], required=True)
    ap.add_argument("--out", required=True, help="memcore-out directory (cache, ledger, token and port files)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--cap", type=int, default=200)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    require_loopback(a.host)
    cache, ledger = os.path.join(a.out, "cache", a.backend), os.path.join(a.out, "ledger.jsonl")
    if a.backend == "claude-cli":
        from .adapters.claude_cli import ClaudeCliChat
        backend = ClaudeCliChat(cwd=os.path.join(a.out, "neutral"), model=a.model or "sonnet", cache_dir=cache, ledger_path=ledger, cap=a.cap)
    else:
        from .adapters.codex_cli import CodexCliChat
        backend = CodexCliChat(cwd=os.path.join(a.out, "neutral"), model=a.model, cache_dir=cache, ledger_path=ledger, cap=a.cap)
    gw = Gateway(backend, a.out, a.host, a.port)
    print(BANNER % (gw.host, gw.port), file=sys.stderr)
    print("base_url=%s token_file=%s stop_file=%s" % (gw.base_url, os.path.join(a.out, "gateway.token"), gw.stop_file), file=sys.stderr)
    gw.start()
    while os.path.exists(gw.stop_file) is False:
        time.sleep(0.5)
    time.sleep(1.0)


if __name__ == "__main__":
    main()
