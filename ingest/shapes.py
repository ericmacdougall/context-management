"""Provider shapes -> role-tagged blocks. Provenance is taken from the PORT STRUCTURE (message role, block type), never from text.

Provenance values: system | user | assistant | assistant_thinking | tool | server_tool | memory_injected | unknown.
Anthropic Messages: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls,
  .../build-with-claude/thinking, .../build-with-claude/streaming (checked 2026-11-16).
OpenAI chat: openai-python types chat_completion_message.py, chat_completion.py, chat_completion_tool_message_param.py (checked
  2026-11-16); the tool_calls union members (id, type, function.name, function.arguments) are NOT fetched: UNVERIFIED here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .canon import cj

ANTHROPIC, OPENAI = "anthropic", "openai"

# server-side tool blocks: produced by the provider, not by the assistant's own words and not by the host's tools
_ANTH_SERVER_CALL = {"server_tool_use", "mcp_tool_use"}
_ANTH_SERVER_RESULT_SUFFIX = "_tool_result"          # web_search_tool_result, web_fetch_tool_result, code_execution_tool_result, mcp_tool_result...

COMPLETE_STOPS = {ANTHROPIC: {"end_turn", "stop_sequence", "tool_use"}, OPENAI: {"stop", "tool_calls"}}


@dataclass
class Blk:
    prov: str
    kind: str
    data: Any
    tool_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def _blocks_of_content(content: Any) -> List[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def classify_anthropic(role: str, b: Any) -> Tuple[str, str, Optional[str]]:
    t = b.get("type") if isinstance(b, dict) else None
    if role == "user":
        if t == "text":
            return "user", "text", None
        if t in ("image", "document"):
            return "user", "media", None
        if t == "tool_result":
            return "tool", "tool_result", b.get("tool_use_id")
    elif role == "assistant":
        if t == "text":
            return "assistant", "text", None
        if t == "tool_use":
            return "assistant", "tool_call", b.get("id")
        if t in ("thinking", "redacted_thinking"):
            return "assistant_thinking", t, None
        if t in _ANTH_SERVER_CALL:
            return "assistant", "server_tool_call", b.get("id")
        if isinstance(t, str) and t.endswith(_ANTH_SERVER_RESULT_SUFFIX):
            return "server_tool", t, b.get("tool_use_id")
    elif role == "system" and t == "text":
        return "system", "text", None
    return "unknown", str(t), None          # fail safe: an unrecognised block is never guessed into user or assistant


def blocks_of_message(provider: str, msg: dict) -> List[Blk]:
    out: List[Blk] = []
    if provider == ANTHROPIC:
        role = msg.get("role")
        for b in _blocks_of_content(msg.get("content")):
            p, k, tid = classify_anthropic(role, b)
            out.append(Blk(p, k, b, tid))
        return out
    role = msg.get("role")
    c = msg.get("content")
    parts = _blocks_of_content(c) if c is not None else []
    for part in parts:
        t = part.get("type") if isinstance(part, dict) else None
        if role in ("system", "developer"):
            out.append(Blk("system", "text" if t == "text" else str(t), part))
        elif role == "user":
            out.append(Blk("user", "text" if t == "text" else "media" if t in ("image_url", "input_audio", "file") else str(t), part)
                       if t in ("text", "image_url", "input_audio", "file") else Blk("unknown", str(t), part))
        elif role == "assistant":
            out.append(Blk("assistant", "text" if t == "text" else "refusal" if t == "refusal" else str(t), part)
                       if t in ("text", "refusal") else Blk("unknown", str(t), part))
        elif role == "tool":
            out.append(Blk("tool", "tool_result", part, msg.get("tool_call_id")))
        else:
            out.append(Blk("unknown", "role:%s" % role, part))
    if role == "assistant":
        if isinstance(msg.get("refusal"), str):
            out.append(Blk("assistant", "refusal", {"type": "refusal", "refusal": msg["refusal"]}))
        for tc in msg.get("tool_calls") or []:
            out.append(Blk("assistant", "tool_call", tc, tc.get("id") if isinstance(tc, dict) else None))
        if msg.get("function_call"):
            out.append(Blk("unknown", "legacy_function_call", msg["function_call"]))
    if role == "tool" and not parts:
        out.append(Blk("tool", "tool_result", {"type": "text", "text": ""}, msg.get("tool_call_id")))
    return out


def _simplify_anth(b: Any) -> Any:
    if not isinstance(b, dict):
        return b
    t = b.get("type")
    if t in ("thinking", "redacted_thinking"):
        return None
    d = {k: v for k, v in b.items() if k not in ("cache_control", "citations")}
    if t == "tool_result" and isinstance(d.get("content"), list):
        d["content"] = [_simplify_anth(x) for x in d["content"]]
    return d


def message_key(provider: str, msg: dict) -> str:
    """Normalised content identity used to match history across calls: ignores cache_control, citations and (Anthropic) thinking
    blocks, which hosts legitimately drop or re-send. Computed on the excised, redacted message."""
    if provider == ANTHROPIC:
        bl = [_simplify_anth(b) for b in _blocks_of_content(msg.get("content"))]
        return cj({"r": msg.get("role"), "b": [b for b in bl if b is not None]})
    d = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "tool_call_id", "refusal", "name")}
    if isinstance(d.get("content"), list):          # one text part == plain string
        parts = d["content"]
        if len(parts) == 1 and isinstance(parts[0], dict) and parts[0].get("type") == "text":
            d["content"] = parts[0].get("text")
    if d.get("role") == "developer":
        d["role"] = "system"
    return cj(d)


def split_request(provider: str, req: dict) -> Tuple[Optional[Any], List[dict]]:
    """(system, messages). Anthropic carries `system` at top level; OpenAI keeps system/developer messages in the list."""
    return (req.get("system") if provider == ANTHROPIC else None), list(req.get("messages") or [])


def response_message(provider: str, resp: dict) -> dict:
    if provider == ANTHROPIC:
        return {"role": "assistant", "content": resp.get("content") or []}
    ch = resp.get("choices") or []
    if len(ch) != 1:
        raise ValueError("choices must have exactly one entry (n>1 unsupported by the tap)")
    return dict(ch[0].get("message") or {})


def stop_info(provider: str, resp: dict) -> Tuple[Optional[str], bool]:
    """(stop_reason, turn_complete). A complete STREAM is not necessarily a complete TURN: max_tokens/length, refusal, pause_turn,
    content_filter end the stream normally but the model's answer is cut or withheld."""
    if provider == ANTHROPIC:
        sr = resp.get("stop_reason")
    else:
        ch = resp.get("choices") or [{}]
        sr = ch[0].get("finish_reason")
    return sr, sr in COMPLETE_STOPS[provider]


def check_pairing(provider: str, messages: List[dict]) -> List[str]:
    """Structural tool pairing violations in a request (the provider would reject most of these; we record, not repair)."""
    v: List[str] = []
    if provider == ANTHROPIC:
        for i, m in enumerate(messages):
            if m.get("role") == "assistant":
                uses = [b.get("id") for b in _blocks_of_content(m.get("content")) if isinstance(b, dict) and b.get("type") == "tool_use"]
                if uses and i + 1 < len(messages):
                    got = {b.get("tool_use_id") for b in _blocks_of_content(messages[i + 1].get("content"))
                           if isinstance(b, dict) and b.get("type") == "tool_result"}
                    for u in uses:
                        if u not in got:
                            v.append("tool_use %s has no tool_result in the next message" % u)
            elif m.get("role") == "user":
                res = [b.get("tool_use_id") for b in _blocks_of_content(m.get("content")) if isinstance(b, dict) and b.get("type") == "tool_result"]
                prev = {b.get("id") for b in _blocks_of_content(messages[i - 1].get("content")) if isinstance(b, dict) and b.get("type") == "tool_use"} \
                    if i > 0 and messages[i - 1].get("role") == "assistant" else set()
                for r in res:
                    if r not in prev:
                        v.append("tool_result %s has no tool_use in the previous assistant message" % r)
                # tool_result blocks must come first, text after (docs: handle-tool-calls)
                seen_text = False
                for b in _blocks_of_content(m.get("content")):
                    if isinstance(b, dict) and b.get("type") == "text":
                        seen_text = True
                    elif isinstance(b, dict) and b.get("type") == "tool_result" and seen_text:
                        v.append("tool_result after text in one user message")
                        break
    else:
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and m.get("tool_calls") and i + 1 < len(messages):
                ids = [tc.get("id") for tc in m["tool_calls"]]
                j, got = i + 1, set()
                while j < len(messages) and messages[j].get("role") == "tool":
                    got.add(messages[j].get("tool_call_id"))
                    j += 1
                for t in ids:
                    if t not in got:
                        v.append("tool_call %s has no tool message" % t)
            if m.get("role") == "tool":
                k = i - 1
                while k >= 0 and messages[k].get("role") == "tool":
                    k -= 1
                if k < 0 or not any(tc.get("id") == m.get("tool_call_id") for tc in (messages[k].get("tool_calls") or [])):
                    v.append("tool message %s has no matching tool_call" % m.get("tool_call_id"))
    return v


# ---------------------------------------------------------------- stream accumulation
class StreamError(Exception):
    """The provider reported an error event, or the stream ended without its terminal event."""


class AnthropicAcc:
    """Folds Anthropic SSE events (as dicts) into a Message. complete only after message_stop. A block without content_block_stop
    is flagged; a tool_use whose partial JSON does not parse is flagged `incomplete` and must never be ingested as a tool call."""

    def __init__(self):
        self.msg: Dict[str, Any] = {}
        self.blocks: Dict[int, dict] = {}
        self.json_buf: Dict[int, str] = {}
        self.closed: set = set()
        self.stop_seen = False
        self.error: Optional[dict] = None

    def feed(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "message_start":
            self.msg = dict(ev.get("message") or {})
        elif t == "content_block_start":
            self.blocks[ev["index"]] = dict(ev.get("content_block") or {})
            self.json_buf[ev["index"]] = ""
        elif t == "content_block_delta":
            i, d = ev["index"], ev.get("delta") or {}
            b = self.blocks.setdefault(i, {})
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "input_json_delta":
                self.json_buf[i] = self.json_buf.get(i, "") + d.get("partial_json", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = d.get("signature", "")
            elif dt == "citations_delta":
                b.setdefault("citations", []).append(d.get("citation"))
        elif t == "content_block_stop":
            self.closed.add(ev["index"])
        elif t == "message_delta":
            d = ev.get("delta") or {}
            for k in ("stop_reason", "stop_sequence"):
                if k in d:
                    self.msg[k] = d[k]
            if ev.get("usage"):
                self.msg["usage"] = {**(self.msg.get("usage") or {}), **ev["usage"]}
        elif t == "message_stop":
            self.stop_seen = True
        elif t == "error":
            self.error = ev.get("error")

    def result(self) -> Tuple[dict, List[int]]:
        """(message dict, indexes of incomplete blocks)."""
        import json as _j
        content, bad = [], []
        for i in sorted(self.blocks):
            b = dict(self.blocks[i])
            if b.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                try:
                    b["input"] = _j.loads(self.json_buf.get(i) or "{}")
                    ok = i in self.closed
                except ValueError:
                    b["input_partial_json"] = self.json_buf.get(i, "")
                    ok = False
                if not ok:
                    bad.append(len(content))
            elif i not in self.closed:
                bad.append(len(content))
            content.append(b)
        m = dict(self.msg)
        m["content"] = content
        return m, bad


class OpenAIAcc:
    def __init__(self):
        self.msg: Dict[str, Any] = {"role": "assistant", "content": None}
        self.tc: Dict[int, dict] = {}
        self.finish: Optional[str] = None
        self.id: Optional[str] = None
        self.done = False
        self.error: Optional[dict] = None

    def feed(self, ch: Any) -> None:
        if ch == "[DONE]":
            self.done = True
            return
        if "error" in ch:
            self.error = ch["error"]
            return
        self.id = ch.get("id", self.id)
        for c in ch.get("choices") or []:
            d = c.get("delta") or {}
            if d.get("content") is not None:
                self.msg["content"] = (self.msg["content"] or "") + d["content"]
            if d.get("refusal") is not None:
                self.msg["refusal"] = (self.msg.get("refusal") or "") + d["refusal"]
            for t in d.get("tool_calls") or []:
                e = self.tc.setdefault(t["index"], {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                e["id"] = t.get("id") or e["id"]
                f = t.get("function") or {}
                e["function"]["name"] += f.get("name") or ""
                e["function"]["arguments"] += f.get("arguments") or ""
            if c.get("finish_reason"):
                self.finish = c["finish_reason"]

    def result(self) -> Tuple[dict, List[int]]:
        import json as _j
        m = dict(self.msg)
        bad = []
        if self.tc:
            m["tool_calls"] = [self.tc[i] for i in sorted(self.tc)]
            for n, i in enumerate(sorted(self.tc)):
                try:
                    _j.loads(self.tc[i]["function"]["arguments"] or "{}")
                except ValueError:
                    bad.append(n)
        return {"id": self.id, "choices": [{"message": m, "finish_reason": self.finish}]}, bad
