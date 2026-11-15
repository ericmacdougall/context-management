"""Minimal stdio MCP tool server (hand-written JSON-RPC, stdlib only): newline-delimited messages, logs to stderr.

Supports the initialize handshake of the 2024-11-05 .. 2025-11-25 era (echoes the client's protocolVersion when we
know it) and also answers tools/list and tools/call without a prior initialize (the 2026-07-28 revision carries
protocol metadata per request). Run: python3 -m memcore.mcp_server
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from .memtools import InMemoryStore, memory_tools
from .ports import AgentToolSurface

KNOWN_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
SERVER_INFO = {"name": "memcore-memory", "version": "0.1.0"}


def demo_store() -> InMemoryStore:
    s = InMemoryStore()
    s.add_event("ev1", "s1", "2026-01-05T10:00:00Z", "Priya said the staging database lives in Frankfurt.", ["Priya"])
    s.add_event("ev2", "s1", "2026-03-02T09:00:00Z", "Priya moved the staging database to Dublin.", ["Priya"])
    s.add_fact("f1", "staging database", "region", "Frankfurt", "2026-01-05T10:00:00Z", "2026-03-02T09:00:00Z", "ev1")
    s.add_fact("f2", "staging database", "region", "Dublin", "2026-03-02T09:00:00Z", None, "ev2")
    return s


def handle(surface: AgentToolSurface, msg: dict) -> Optional[dict]:
    """Return a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification (initialized, cancelled, ...)

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in KNOWN_VERSIONS else KNOWN_VERSIONS[-2],
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": surface.list_tools()})
    if method == "tools/call":
        try:
            res = surface.call_tool(params.get("name"), params.get("arguments"))
        except KeyError as e:
            return err(-32602, str(e))
        except ValueError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}], "isError": False})
    return err(-32601, "method not found: %s" % method)


def serve(surface: AgentToolSurface, fin=sys.stdin, fout=sys.stdout) -> None:
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            fout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            fout.flush()
            continue
        resp = handle(surface, msg)
        if resp is not None:
            fout.write(json.dumps(resp) + "\n")
            fout.flush()


if __name__ == "__main__":
    print("memcore MCP stdio server (fake in-memory store)", file=sys.stderr)
    serve(memory_tools(demo_store()))
