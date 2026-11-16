"""Scripted model stubs (NO model calls). A stub is a transport: .call(req) -> provider response dict, .stream(req) -> event iterator.
The script is a list of specs consumed one per call:
  {"content": [blocks] | "message": {...openai message}, "stop": "end_turn", "fail": None | "reject" | "timeout" | "drop:N" | "error:N" | "silent:N" | "badjson"}
The stub writes ground truth (what it received and what it completed) to an fsynced file, outside the tap."""
from __future__ import annotations

import copy
import json
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

from .tap import Rejected


class Truth:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.mem: List[dict] = []

    def add(self, **kw) -> None:
        self.mem.append(kw)
        if self.path:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(kw) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)


def anth_msg(content: List[dict], stop: str = "end_turn", mid: str = "msg_stub_0", model: str = "stub-model") -> dict:
    return {"id": mid, "type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


def anth_events(msg: dict, chunk: int = 6) -> List[dict]:
    evs = [{"type": "message_start", "message": {**{k: v for k, v in msg.items() if k not in ("content", "stop_reason")},
                                                  "content": [], "stop_reason": None}}]
    for i, b in enumerate(msg["content"]):
        t = b["type"]
        if t == "text":
            evs.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            for j in range(0, len(b["text"]), chunk):
                evs.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"][j:j + chunk]}})
        elif t in ("tool_use", "server_tool_use"):
            evs.append({"type": "content_block_start", "index": i, "content_block": {**{k: v for k, v in b.items() if k != "input"}, "input": {}}})
            js = json.dumps(b["input"])
            for j in range(0, len(js), chunk):
                evs.append({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": js[j:j + chunk]}})
        elif t == "thinking":
            evs.append({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": ""}})
            for j in range(0, len(b["thinking"]), chunk):
                evs.append({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"][j:j + chunk]}})
            evs.append({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
        else:                                   # redacted_thinking, *_tool_result: delivered whole in content_block_start
            evs.append({"type": "content_block_start", "index": i, "content_block": b})
        evs.append({"type": "content_block_stop", "index": i})
    evs.append({"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 5}})
    evs.append({"type": "message_stop"})
    return evs


def oai_resp(message: dict, finish: str = "stop", rid: str = "chatcmpl-stub-0") -> dict:
    return {"id": rid, "object": "chat.completion", "model": "stub-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}


def oai_chunks(resp: dict, chunk: int = 6) -> List[Any]:
    m = resp["choices"][0]["message"]
    base = {"id": resp["id"], "object": "chat.completion.chunk"}
    out: List[Any] = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
    c = m.get("content") or ""
    for j in range(0, len(c), chunk):
        out.append({**base, "choices": [{"index": 0, "delta": {"content": c[j:j + chunk]}, "finish_reason": None}]})
    for ti, tc in enumerate(m.get("tool_calls") or []):
        out.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": ti, "id": tc["id"], "type": "function",
                    "function": {"name": tc["function"]["name"], "arguments": ""}}]}, "finish_reason": None}]})
        a = tc["function"]["arguments"]
        for j in range(0, len(a), chunk):
            out.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": ti, "function": {"arguments": a[j:j + chunk]}}]},
                                             "finish_reason": None}]})
    out.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": resp["choices"][0]["finish_reason"]}]})
    out.append("[DONE]")
    return out


class Stub:
    """provider: 'anthropic'|'openai'. delay_s: per non-stream call; event_delay_s: per stream event (indicative-cost runs)."""

    def __init__(self, provider: str, script: List[dict], truth: Optional[Truth] = None, delay_s: float = 0.0, event_delay_s: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.provider, self.script, self.truth = provider, list(script), truth or Truth(None)
        self.delay_s, self.event_delay_s, self.sleep = delay_s, event_delay_s, sleep
        self.n = 0
        self.received: List[dict] = []

    def _next(self, req: dict) -> dict:
        self.n += 1
        self.received.append(copy.deepcopy(req))
        spec = self.script[self.n - 1] if self.n - 1 < len(self.script) else self.script[-1]
        if spec.get("fail") == "reject":
            raise Rejected("400 invalid_request")
        self.truth.add(ev="received", n=self.n, msgs=len(req.get("messages") or []))
        return spec

    def _resp(self, spec: dict) -> dict:
        if self.provider == "anthropic":
            return anth_msg(spec["content"], spec.get("stop", "end_turn"), spec.get("id", "msg_stub_%d" % self.n))
        return oai_resp(spec["message"], spec.get("stop", "stop"), spec.get("id", "chatcmpl-stub-%d" % self.n))

    def call(self, req: dict) -> dict:
        spec = self._next(req)
        if self.delay_s:
            self.sleep(self.delay_s)
        if spec.get("fail") == "timeout":
            raise TimeoutError("stub timeout")
        resp = self._resp(spec)
        self.truth.add(ev="completed", n=self.n, id=resp["id"])
        return resp

    def stream(self, req: dict) -> Iterator[Any]:
        spec = self._next(req)
        resp = self._resp(spec)
        evs = anth_events(resp) if self.provider == "anthropic" else oai_chunks(resp)
        fail = spec.get("fail") or ""
        kind, _, arg = fail.partition(":")
        limit = int(arg) if arg else None
        if kind == "badjson":                      # content_block_stop never sent: the tool_use JSON is unterminated, stream still closes
            evs = [e for e in evs if not (isinstance(e, dict) and e.get("type") == "content_block_stop")]
        for i, ev in enumerate(evs):
            if limit is not None and i >= limit:
                if kind == "drop":
                    raise ConnectionError("stub connection dropped")
                if kind == "error":
                    yield {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}} if self.provider == "anthropic" \
                        else {"error": {"type": "server_error", "message": "boom"}}
                    return
                if kind == "silent":
                    return
            if self.event_delay_s:
                self.sleep(self.event_delay_s)
            yield ev
        self.truth.add(ev="completed", n=self.n, id=resp["id"])


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_use(i: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def tool_result(i: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": i, "content": content}


def thinking(t: str, sig: str) -> dict:
    return {"type": "thinking", "thinking": t, "signature": sig}
