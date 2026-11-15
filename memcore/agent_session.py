"""AgentSession port: a long-lived agent session you can send turns to and push out-of-band context into.

  open(system, tools=[McpServerSpec...], model) -> session          (via a factory, one per adapter)
  session.send(text) -> iterator of SessionEvent, the last one kind="result" carrying a TurnResult
  session.deliver(items)  queue InboxItems; they are rendered (inbox.render_block: delimited, quote JSON-encoded, per-turn
                          random delimiter) and placed at the START of the next send(), i.e. at the turn boundary. Nothing is
                          ever injected mid-turn.
  session.close()

Adapters here: FakeAgentSession (deterministic, tests), ClaudeStreamJsonSession (one `claude -p` process, stream-json in and
out), AcpSession (Agent Client Protocol client over stdio to an ACP agent process; see acp.py).
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .inbox import InboxItem, render_block
from .budget import CallBudget
from .devgate import require_dev_subscription
from .ports import BackendError, CallTimeout, Usage


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class SessionEvent:
    kind: str            # "text" | "tool_call" | "tool_result" | "result" | "raw"
    data: Any = None


@dataclass
class TurnResult:
    text: str
    stop_reason: str
    usage: Optional[Usage]           # this turn only when the backend reports per-turn usage, else None
    seconds: float
    turn_id: str
    delivered_ids: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


class AgentSession:
    session_id: str = ""
    _pending: List[InboxItem]

    budget: Optional[CallBudget] = None

    def _count_turn(self, turn_id: str) -> None:
        """Every turn is one counted, capped, ledgered call (sessions bypassed the cap before the review)."""
        if self.budget is None:
            raise BackendError("session has no CallBudget")
        self.budget.reserve()
        self.budget.ledger(kind="session_turn", session=self.session_id, turn=turn_id, adapter=type(self).__name__)

    def _take_context(self, turn_id: str) -> str:
        items, self._pending = self._pending, []
        self._last_delivered = [i.finding_id for i in items]
        return render_block(items, turn_id) if items else ""

    def deliver(self, items: Sequence[InboxItem]) -> None:
        self._pending.extend(items)

    def send(self, text: str) -> Iterator[SessionEvent]:
        raise NotImplementedError

    def send_collect(self, text: str) -> TurnResult:
        last = None
        for ev in self.send(text):
            if ev.kind == "result":
                last = ev.data
        if last is None:
            raise BackendError("session ended a turn without a result")
        return last

    def close(self) -> None:
        raise NotImplementedError


class FakeAgentSession(AgentSession):
    """Deterministic: answers 'ECHO <text>' and reports what context block it was shown (for tests)."""

    def __init__(self, system: str = "", tools: Sequence[McpServerSpec] = (), model: str = "fake", budget: Optional[CallBudget] = None):
        self.budget = budget
        self.session_id, self.system, self.tools, self.model = "fake-%d" % next(_ids), system, list(tools), model
        self._pending, self.closed, self.turns, self.shown = [], False, 0, []
        self._last_delivered: List[str] = []

    def send(self, text):
        if self.closed:
            raise BackendError("session closed")
        self.turns += 1
        turn_id = "%s-t%d" % (self.session_id, self.turns)
        if self.budget is not None:
            self._count_turn(turn_id)
        block = self._take_context(turn_id)
        self.shown.append(block)
        reply = "ECHO %s" % text
        yield SessionEvent("text", reply)
        yield SessionEvent("result", TurnResult(reply, "end_turn", Usage(len(text), 0, len(reply)), 0.0, turn_id, self._last_delivered))

    def close(self):
        self.closed = True


_ids = itertools.count(1)

HOOK_LEAK_MARKERS = ("did not write a WAL", "WAL note", "stop hook", "Stop hook")


class ClaudeStreamJsonSession(AgentSession):
    """One long-lived headless Claude Code process. Same safety shape as the single-shot adapter: neutral non-git cwd,
    --tools "" (plus an explicit --allowedTools only for the MCP servers passed in), --strict-mcp-config, no session
    persistence; hooks and setting sources are left untouched."""

    def __init__(self, cwd: str, system: str, budget: CallBudget, dev_subscription_backends: bool = False, tools: Sequence[McpServerSpec] = (), model: str = "sonnet", mcp_config_dir: Optional[str] = None,
                 turn_timeout_s: float = 300.0, claude_bin: str = "claude",
                 strip_patterns: Sequence[str] = ()):
        require_dev_subscription(dev_subscription_backends, "ClaudeStreamJsonSession")
        self.budget = budget
        if os.path.isdir(os.path.join(cwd, ".git")):
            raise ValueError("cwd must be a neutral non-git directory")
        cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--model", model,
               "--tools", "", "--strict-mcp-config", "--system-prompt", system, "--no-session-persistence"]
        if tools:
            cfg = {"mcpServers": {t.name: {"command": t.command, "args": t.args, "env": t.env} for t in tools}}
            p = os.path.join(mcp_config_dir or cwd, "mcp-%d-%d.json" % (os.getpid(), next(_ids)))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # may carry a bearer token: owner-only
            with os.fdopen(fd, "w") as fh:
                json.dump(cfg, fh)
            self._mcp_cfg = p
            cmd += ["--mcp-config", p, "--allowedTools", ",".join("mcp__%s" % t.name for t in tools)]
        import re as _re
        self.model, self.turn_timeout_s = model, turn_timeout_s
        self.strip_patterns, self.filter_hits = [_re.compile(p) for p in strip_patterns], 0
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=cwd,
                                      start_new_session=True, bufsize=1)
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._pending, self.turns, self.session_id, self._last_delivered = [], 0, "", []

    def _reader(self):
        for line in self._proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def send(self, text):
        self.turns += 1
        turn_id = "claude-t%d" % self.turns
        self._count_turn(turn_id)
        block = self._take_context(turn_id)
        content = (block + "\n\n" + text) if block else text
        t0 = time.time()
        try:
            self._proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise BackendError("claude session process is gone")
        deadline = t0 + self.turn_timeout_s
        while True:
            try:
                line = self._q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self.close()
                raise CallTimeout("turn exceeded %.0fs; session killed" % self.turn_timeout_s)
            if line is None:
                raise BackendError("claude session ended unexpectedly")
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("session_id"):
                self.session_id = ev["session_id"]
            elif t == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "text":
                        yield SessionEvent("text", b["text"])
                    elif b.get("type") == "tool_use":
                        yield SessionEvent("tool_call", {"name": b.get("name"), "input": b.get("input")})
            elif t == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        yield SessionEvent("tool_result", b.get("content"))
            elif t == "result":
                if ev.get("is_error"):
                    raise BackendError("claude turn error: %s" % str(ev.get("result"))[:300])
                u = ev.get("usage") or {}
                cr, cc = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
                usage = Usage((u.get("input_tokens") or 0) + cr + cc, cr, u.get("output_tokens")) if u else None
                rtext = ev.get("result") or ""
                for p in self.strip_patterns:
                    rtext, k = p.subn("", rtext)
                    self.filter_hits += k
                yield SessionEvent("result", TurnResult(rtext.strip() if self.strip_patterns else rtext, ev.get("stop_reason") or "end_turn", usage,
                                                        round(time.time() - t0, 2), turn_id, self._last_delivered,
                                                        {"cost_usd": ev.get("total_cost_usd"), "num_turns": ev.get("num_turns"),
                                                         "session_id": ev.get("session_id")}))
                return

    def close(self):
        cfg = getattr(self, "_mcp_cfg", None)
        if cfg:
            try:
                os.unlink(cfg)                                    # the one-off MCP config is removed with the session
            except FileNotFoundError:
                pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._proc.wait()
