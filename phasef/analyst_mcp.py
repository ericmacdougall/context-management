#!/usr/bin/env python3
"""Thin MCP stdio server for the analyst arm (stdlib only, newline-delimited JSON-RPC). Enforces the tool-call budget HERE, not in the agent.
Usage: analyst_mcp.py --budget 12 --log PATH (--fake TASK.json | --backend "ssh host /path/prog")
Backend contract (assumption until the retrieval owner publishes the program): one JSON request on stdin
  {"tool": "<name>", "args": {...}} -> one JSON object on stdout. Every response is scanned for "round_id" values, which the runner treats as ids the agent has legitimately seen."""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

TOOLS = [
    {"name": "search", "description": "Search the memory for rounds matching a query. Returns up to k round ids with date, session id and a snippet.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}},
    {"name": "open_round", "description": "Return the full text of one round by round id.",
     "inputSchema": {"type": "object", "properties": {"round_id": {"type": "string"}}, "required": ["round_id"]}},
    {"name": "open_session", "description": "Return the date and the round ids with snippets of one session by session id.",
     "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]}},
    {"name": "timeline", "description": "List sessions in date order (session id, date, first words), optionally only those whose text mentions a term.",
     "inputSchema": {"type": "object", "properties": {"term": {"type": "string"}}}}]


class Server:
    def __init__(self, budget, log_path, backend):
        self.budget, self.log_path, self.backend, self.used = budget, log_path, backend, 0

    def log(self, rec):
        with open(self.log_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def call_tool(self, name, args):
        if name not in [t["name"] for t in TOOLS]:
            raise KeyError("unknown tool %s" % name)
        if self.used >= self.budget:
            self.log({"tool": name, "args": args, "refused": True, "used": self.used})
            return "TOOL BUDGET EXHAUSTED: %d of %d calls used. Give your final answer now." % (self.used, self.budget), True, []
        self.used += 1
        t0 = time.time()
        try:
            res = self.backend({"tool": name, "args": args or {}})
            err = False
        except Exception as e:  # backend failure is reported to the agent, and still counted
            res, err = {"error": str(e)[:300]}, True
        text = json.dumps(res, ensure_ascii=False)
        ids = sorted(set(re.findall(r'"round_id":\s*"([^"]+)"', text)))
        self.log({"tool": name, "args": args, "refused": False, "used": self.used, "ms": int((time.time() - t0) * 1000), "error": err, "round_ids": ids, "chars": len(text)})
        return text, err, ids

    def handle(self, msg):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}
        if method == "initialize":
            return ok({"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "analyst", "version": "0.1"}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            try:
                text, err, _ = self.call_tool(params.get("name"), params.get("arguments"))
            except KeyError as e:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
            return ok({"content": [{"type": "text", "text": text}], "isError": err})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}


def shell_backend(cmd):
    argv = shlex.split(cmd)

    def run(req):
        p = subprocess.run(argv, input=json.dumps(req), capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError("backend exit %d: %s" % (p.returncode, p.stderr[-200:]))
        return json.loads(p.stdout)
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=12)
    ap.add_argument("--log", required=True)
    ap.add_argument("--fake")
    ap.add_argument("--backend")
    a = ap.parse_args()
    if a.fake:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from phasef.analyst_fake import FakeBackend
        backend = FakeBackend(a.fake)
    else:
        backend = shell_backend(a.backend)
    srv = Server(a.budget, a.log, backend)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = srv.handle(json.loads(line))
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
