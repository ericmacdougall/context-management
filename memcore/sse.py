"""OpenAI-style server-sent-event framing: encode on the gateway, parse on the client."""
from __future__ import annotations

import json
from typing import Iterable, Iterator, List, Optional


def frame(obj) -> bytes:
    """One SSE event: 'data: <json>' followed by a blank line. obj may be the string '[DONE]'."""
    payload = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    return ("data: %s\n\n" % payload).encode("utf-8")


def chat_chunks(cid: str, model: str, created: int, text: str, usage: Optional[dict], piece: int = 48,
                tool_call: Optional[dict] = None) -> List[bytes]:
    """role chunk, content chunks (or one tool-call delta stream), finish chunk, final usage chunk (choices: []), [DONE].
    Every chunk carries id, object, model, created and, per choice, index and delta (what Elasticsearch's openai service requires).
    usage must hold all three counts as ints when present."""
    def c(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    out = [frame(c({"role": "assistant", "content": ""}))]
    if tool_call is not None:
        args = tool_call["arguments"]
        out.append(frame(c({"tool_calls": [{"index": 0, "id": tool_call["id"], "type": "function",
                                            "function": {"name": tool_call["name"], "arguments": ""}}]})))
        for i in range(0, len(args), piece):
            out.append(frame(c({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + piece]}}]})))
        out.append(frame(c({}, "tool_calls")))
    else:
        for i in range(0, len(text), piece):
            out.append(frame(c({"content": text[i:i + piece]})))
        out.append(frame(c({}, "stop")))
    if usage is not None:
        out.append(frame({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                          "choices": [], "usage": usage}))
    out.append(frame("[DONE]"))
    return out


def parse_events(lines: Iterable[bytes]) -> Iterator[object]:
    """Yield parsed JSON objects (or the string '[DONE]') from a byte-line iterator; events end at a blank line."""
    data: List[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                yield "[DONE]" if payload == "[DONE]" else json.loads(payload)
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        # comments (':') and other fields are ignored
    if data:
        raise ValueError("stream ended inside an event (missing blank line)")


def parse_named_events(lines: Iterable[bytes]) -> Iterator[tuple]:
    """Yield (event_name, data_string) pairs. Strips a UTF-8 byte-order mark at the start of every event (Elasticsearch 9.5
    emits one before each 'event:' line, per the research; hexdump to confirm on the node). Name defaults to 'message'."""
    name, data = None, []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        line = line.lstrip("﻿")
        if line == "":
            if data or name:
                yield (name or "message", "\n".join(data))
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data or name:
        yield (name or "message", "\n".join(data))
