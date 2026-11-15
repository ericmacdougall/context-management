"""ChatModel for Elasticsearch's inference API (the customer route): POST /_inference/chat_completion/{id}/_stream.

On 9.5 only the streaming form exists (non-streaming is 9.6+, research ES 9.5 source). The request parser is strict: an unknown
field (for example response_format) is a 400, so this adapter sends only messages, tools, tool_choice, temperature and
max_completion_tokens. Structured output = ONE forced function tool whose parameters are the JSON schema; the arguments are
validated client-side by the shared base (plus one repair retry). SSE: a BOM may precede every event; a chunk is bare or wrapped
in {"chat_completion": {...}}; stop at [DONE]; 'event: error' becomes BackendError.
Credentials: the caller supplies `auth_header` (a callable returning the Authorization value); it is never stored in
the ledger, cache or logs, and this class never reads files itself. Not subscription backed.
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Callable, Optional

from ..chat_base import BackendOut, CachedChatModel
from ..ports import BackendError, CallTimeout, Capabilities, InputTooLong, Usage
from ..sse import parse_named_events

TOOL_NAME = "answer"


def unwrap(d: dict) -> dict:
    return d["chat_completion"] if isinstance(d, dict) and isinstance(d.get("chat_completion"), dict) else d


class ElasticInferenceChat(CachedChatModel):
    adapter_id = "elastic-inference"

    def __init__(self, base_url: str, endpoint_id: str, auth_header: Callable[[], str], model: Optional[str] = None,
                 ca_file: Optional[str] = None, verify_tls: bool = True, max_context_tokens: Optional[int] = None, **kw):
        self.base_url, self.endpoint_id, self._auth = base_url.rstrip("/"), endpoint_id, auth_header
        self._ctx = None
        if base_url.startswith("https"):
            self._ctx = ssl.create_default_context(cafile=ca_file)
            if not verify_tls:  # explicit, named opt-out for a local self-signed trial node
                self._ctx.check_hostname, self._ctx.verify_mode = False, ssl.CERT_NONE
        super().__init__(model=model or endpoint_id, **kw)
        self.capabilities = Capabilities(adapter=self.adapter_id, supports_json_schema=True, json_schema_mode="forced-tool+validate",
                                         supports_system=True, reports_usage=True, max_context_tokens=max_context_tokens,
                                         supports_temperature=True, supports_max_output_tokens=True, streaming="native")

    def body(self, prompt, system, json_schema, max_output_tokens=None, temperature=None) -> dict:
        msgs = ([{"role": "system", "content": system}] if system is not None else []) + [{"role": "user", "content": prompt}]
        b = {"messages": msgs}
        if json_schema is not None:
            b["tools"] = [{"type": "function", "function": {"name": TOOL_NAME, "description": "Return the answer.", "parameters": json_schema}}]
            b["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
        if max_output_tokens is not None:
            b["max_completion_tokens"] = max_output_tokens
        if temperature is not None:
            b["temperature"] = temperature
        return b

    def _call_backend(self, prompt, system, json_schema, timeout_s) -> BackendOut:
        url = "%s/_inference/chat_completion/%s/_stream" % (self.base_url, self.endpoint_id)
        req = urllib.request.Request(url, data=json.dumps(self.body(prompt, system, json_schema)).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": self._auth()})
        try:
            r = urllib.request.urlopen(req, timeout=timeout_s, context=self._ctx)
        except urllib.error.HTTPError as e:
            raw = e.read()[:1500].decode("utf-8", "replace")
            if "context_length" in raw or "maximum context" in raw:
                raise InputTooLong(raw[:200])
            raise BackendError("HTTP %d: %s" % (e.code, raw[:300]))
        except (socket.timeout, TimeoutError):
            raise CallTimeout("no response within %.1fs" % timeout_s)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise CallTimeout("no response within %.1fs" % timeout_s)
            raise BackendError("connection failed: %s" % e.reason)
        return self.read_stream(r)

    def read_stream(self, lines) -> BackendOut:
        text, args, usage, finish = [], {}, None, None
        try:
            for name, data in parse_named_events(lines):
                if name == "error":
                    raise BackendError("inference stream error: %s" % data[:300])
                if data.strip() == "[DONE]":
                    break
                if not data.strip():
                    continue
                d = unwrap(json.loads(data))
                if d.get("usage"):
                    u = d["usage"]
                    usage = Usage(u.get("prompt_tokens"), (u.get("prompt_tokens_details") or {}).get("cached_tokens"), u.get("completion_tokens"))
                for c in d.get("choices") or []:
                    delta = c.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        args.setdefault(tc.get("index", 0), []).append((tc.get("function") or {}).get("arguments") or "")
                    finish = c.get("finish_reason") or finish
        except (socket.timeout, TimeoutError):
            raise CallTimeout("stream stalled")
        out = "".join(text) if not args else "".join("".join(v) for _, v in sorted(args.items()))
        return BackendOut(text=out, usage=usage, meta={"finish_reason": finish, "forced_tool": bool(args)})
