"""ChatModel for the first-party Anthropic Messages API (API-key route; the default-config alternative to the dev subscription CLIs).
Code plus stub tests only: NO key is looked up (no env, no file); the caller passes api_key() and base_url. Never called live here.
Structured output = one forced tool (tool_choice {"type":"tool","name":...}) validated client side by the shared base."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Callable, Optional

from ..chat_base import BackendOut, CachedChatModel
from ..http_util import urlopen as _urlopen
from ..ports import BackendError, CallTimeout, Capabilities, InputTooLong, Usage

TOOL = "answer"


class AnthropicMessagesChat(CachedChatModel):
    adapter_id = "anthropic-api"

    def __init__(self, model: str, api_key: Callable[[], str], base_url: str = "https://api.anthropic.com", version: str = "2023-06-01",
                 default_max_tokens: int = 4096, max_context_tokens: Optional[int] = 200_000, **kw):
        self._key, self.base_url, self.version, self.default_max_tokens = api_key, base_url.rstrip("/"), version, default_max_tokens
        super().__init__(model=model, **kw)
        self.capabilities = Capabilities(adapter=self.adapter_id, supports_json_schema=True, json_schema_mode="forced-tool+validate", supports_system=True,
                                         reports_usage=True, max_context_tokens=max_context_tokens, supports_temperature=True,
                                         supports_max_output_tokens=True, streaming="none")

    def settings_fingerprint(self):
        return {"version": self.version, "default_max_tokens": self.default_max_tokens}

    def body(self, prompt, system, json_schema, max_output_tokens=None, temperature=None) -> dict:
        b = {"model": self.model, "max_tokens": max_output_tokens or self.default_max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system is not None:
            b["system"] = system
        if temperature is not None:
            b["temperature"] = temperature
        if json_schema is not None:
            b["tools"] = [{"name": TOOL, "description": "Return the answer.", "input_schema": json_schema}]
            b["tool_choice"] = {"type": "tool", "name": TOOL}
        return b

    def _call_backend(self, prompt, system, json_schema, timeout_s, params=None) -> BackendOut:
        p = params or {}
        req = urllib.request.Request(self.base_url + "/v1/messages", data=json.dumps(self.body(prompt, system, json_schema, p.get("max_output_tokens"), p.get("temperature"))).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-api-key": self._key(), "anthropic-version": self.version})
        try:
            with _urlopen(req, timeout=timeout_s) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()[:1500].decode("utf-8", "replace")
            if e.code == 400 and "too long" in raw or "context" in raw and e.code == 400:
                raise InputTooLong(raw[:200])
            raise BackendError("HTTP %d: %s" % (e.code, raw[:300])) from None
        except (socket.timeout, TimeoutError):
            raise CallTimeout("no response within %.1fs" % timeout_s)
        except urllib.error.URLError as e:
            raise BackendError("connection failed: %s" % e.reason)
        text, parsed = "", None
        for blk in d.get("content") or []:
            if blk.get("type") == "text":
                text += blk.get("text", "")
            elif blk.get("type") == "tool_use":
                parsed = blk.get("input")
                text = json.dumps(parsed)
        u = d.get("usage") or {}
        cr, cc = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
        usage = Usage((u.get("input_tokens") or 0) + cr + cc, cr, u.get("output_tokens")) if u else None
        return BackendOut(text=text, parsed=parsed, usage=usage, meta={"stop_reason": d.get("stop_reason"), "upstream_model": d.get("model")})
