"""Agent Client Protocol (https://agentclientprotocol.com, JSON-RPC 2.0 over stdio, protocolVersion 1) client and an
AgentSession adapter on top of it. NOT the Agent Communication Protocol (IBM/BeeAI, folded into A2A): different thing.

The client launches an ACP agent as a subprocess, does initialize -> session/new, then session/prompt per turn, collecting
session/update notifications (agent_message_chunk, usage_update, tool_call) until the prompt response (stopReason).
Permission requests are answered 'cancelled' (nothing is granted); fs/terminal capabilities are not advertised.
Authentication is whatever the agent process already has (for claude-agent-acp: the existing Claude login; no API key set here).
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .budget import CallBudget
from .agent_session import AgentSession, McpServerSpec, SessionEvent, TurnResult
from .devgate import require_dev_subscription
from .ports import BackendError, CallTimeout, Usage


class AcpClient:
    subscription_backed = True        # never served over HTTP (gateway.check_gate); decision of 2026-11-16

    def __init__(self, cmd: List[str], cwd: str, env: Optional[dict] = None, dev_subscription_backends: bool = False):
        require_dev_subscription(dev_subscription_backends, "AcpClient")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=cwd,
                                     env=env, start_new_session=True, bufsize=1)
        self._id, self._w = 0, threading.Lock()
        self.q: "queue.Queue[Optional[dict]]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put(None)

    def _send(self, obj: dict) -> None:
        with self._w:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout_s: float, on_update=None) -> Any:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout_s
        while True:
            try:
                m = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self.kill()
                raise CallTimeout("%s exceeded %.0fs; agent killed" % (method, timeout_s))
            if m is None:
                raise BackendError("ACP agent process ended")
            if m.get("id") == rid and ("result" in m or "error" in m):
                if "error" in m:
                    raise BackendError("ACP error on %s: %s" % (method, json.dumps(m["error"])[:300]))
                return m.get("result")
            if m.get("method") == "session/request_permission":  # we grant nothing
                self._send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": {"outcome": "cancelled"}}})
            elif "id" in m and "method" in m:  # any other agent->client request: not supported
                self._send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported by this client"}})
            elif m.get("method") == "session/update" and on_update:
                on_update(m["params"])

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.kill()


class AcpSession(AgentSession):
    subscription_backed = True        # never served over HTTP (gateway.check_gate); decision of 2026-11-16

    def __init__(self, cmd: List[str], cwd: str, budget: CallBudget, tools: Sequence[McpServerSpec] = (), dev_subscription_backends: bool = False, turn_timeout_s: float = 300.0, env: Optional[dict] = None,
                 model: Optional[str] = "sonnet", mode: Optional[str] = "default"):
        require_dev_subscription(dev_subscription_backends, "AcpSession")
        self.budget = budget
        self.cli, self.turn_timeout_s, self.turns = AcpClient(cmd, cwd, env, dev_subscription_backends=True), turn_timeout_s, 0
        init = self.cli.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "memcore", "version": "0.1"}}, 60)
        self.agent_caps = (init or {}).get("agentCapabilities", {})
        self.auth_methods = (init or {}).get("authMethods", [])
        mcp = [{"name": t.name, "command": t.command, "args": t.args, "env": [{"name": k, "value": v} for k, v in t.env.items()]} for t in tools]
        self.session_id = self.cli.request("session/new", {"cwd": cwd, "mcpServers": mcp}, 120)["sessionId"]
        self.config_errors = []
        for cid, val in (("model", model), ("mode", mode)):  # claude-agent-acp advertises these as session config options
            if val:
                try:
                    self.cli.request("session/set_config_option", {"sessionId": self.session_id, "configId": cid, "value": val}, 60)
                except BackendError as e:
                    self.config_errors.append(str(e)[:200])
        self._pending, self._last_delivered = [], []

    def send(self, text):
        self._guard(text, "session send text")            # before the turn is counted or pending items are taken
        self.turns += 1
        turn_id = "acp-t%d" % self.turns
        self._count_turn(turn_id)
        content = self._compose(text, turn_id)
        parts, usage, tools, t0 = [], {}, [], time.time()
        events: List[SessionEvent] = []

        def on_update(p):
            u = p.get("update") or {}
            k = u.get("sessionUpdate")
            if k == "agent_message_chunk" and (u.get("content") or {}).get("type") == "text":
                parts.append(u["content"]["text"])
                events.append(SessionEvent("text", u["content"]["text"]))
            elif k == "usage_update":
                usage.update(u)
            elif k in ("tool_call", "tool_call_update"):
                events.append(SessionEvent("tool_call", {"id": u.get("toolCallId"), "title": u.get("title"), "status": u.get("status")}))
        res = self.cli.request("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": content}]},
                               self.turn_timeout_s, on_update)
        for e in events:
            yield e
        yield SessionEvent("result", TurnResult("".join(parts), (res or {}).get("stopReason", "end_turn"), None,
                                                round(time.time() - t0, 2), turn_id, self._last_delivered,
                                                {"acp_usage_update": usage}))

    def close(self):
        self.cli.close()
