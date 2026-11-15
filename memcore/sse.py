"""OpenAI-style server-sent-event framing: encode on the gateway, parse on the client."""
from __future__ import annotations

import json
from typing import Iterable, Iterator, List, Optional


def frame(obj) -> bytes:
    """One SSE event: 'data: <json>' followed by a blank line. obj may be the string '[DONE]'."""
    payload = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    return ("data: %s\n\n" % payload).encode("utf-8")


def chat_chunks(cid: str, model: str, created: int, text: str, usage: Optional[dict], piece: int = 48) -> List[bytes]:
    """role chunk, content chunks, finish chunk, final usage chunk (choices: []), [DONE]."""
    def c(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    out = [frame(c({"role": "assistant", "content": ""}))]
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
