"""MCP over Streamable HTTP, minimal server side (spec transport: a single endpoint, POST of one JSON-RPC message; the server
answers application/json, or 202 with no body for notifications/responses). Stateless (no Mcp-Session-Id issued), no SSE stream
(GET -> 405). Loopback bind only (or one named private address for a container client), bearer token compared in constant time,
token read from a 0600 file, Origin header validated against local origins (DNS-rebinding guard).
Run: python3 -m memcore.mcp_http --token-file F [--port N] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from .mcp_server import demo_store, handle
from .memtools import memory_tools
from .ports import AgentToolSurface

MAX_BODY = 1 << 20


def read_token(path: str) -> str:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise PermissionError("token file %s must be 0600 (is %o)" % (path, mode))
    return open(path).read().strip()


def make_token_file(path: str) -> str:
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def origin_ok(origin: Optional[str], allowed_hosts) -> bool:
    if not origin:
        return True  # non-browser clients send none
    return (urlparse(origin).hostname or "") in allowed_hosts


def make_handler(surface: AgentToolSurface, token: str, allowed_hosts):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            raw = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            if raw:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._send(405, {"error": "this server offers no SSE stream"})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                return self._send(401, {"error": "unauthorized"})
            if not origin_ok(self.headers.get("Origin"), allowed_hosts):
                return self._send(403, {"error": "origin not allowed"})
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"error": "bad length"})
            try:
                msg = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            if not isinstance(msg, dict):
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches not supported"}})
            resp = handle(surface, msg)
            self._send(202) if resp is None else self._send(200, resp)
    return H


class McpHttpServer:
    def __init__(self, surface: AgentToolSurface, token: str, host: str = "127.0.0.1", port: int = 0, allow_private_host: Optional[str] = None):
        ip = ipaddress.ip_address(host)
        if not ip.is_loopback and not (allow_private_host == host and ip.is_private and host not in ("0.0.0.0", "::")):
            raise ValueError("refusing to bind %r: loopback only (or one explicitly named private address)" % host)
        self.srv = ThreadingHTTPServer((host, port), make_handler(surface, token, {"127.0.0.1", "localhost", "::1", host}))
        self.srv.daemon_threads = True
        self.host, self.port = self.srv.server_address[:2]
        self.url = "http://%s:%d/mcp" % (self.host, self.port)

    def start(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", required=True, help="created 0600 if missing")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    tok = read_token(a.token_file) if os.path.exists(a.token_file) else make_token_file(a.token_file)
    srv = McpHttpServer(memory_tools(demo_store()), tok, a.host, a.port, allow_private_host=a.host if a.host != "127.0.0.1" else None).start()
    print("memcore MCP http (fake store): %s  token in %s" % (srv.url, a.token_file), file=sys.stderr, flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
