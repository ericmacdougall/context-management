"""ChatModel client for any OpenAI-compatible /v1/chat/completions endpoint (stdlib urllib).
Non-streaming through complete() (cached/capped/ledgered like every adapter); stream() yields text deltas and
returns usage via the generator's final StopIteration value (see stream_with_usage). Tools are not supported yet.
JSON schema is sent as response_format {type: json_schema} and validated client-side (plus one repair retry).
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Iterator, Optional, Sequence, Tuple

from ..chat_base import BackendOut, CachedChatModel, render_messages
from ..http_util import urlopen as _urlopen
from ..ports import BackendError, CallTimeout, Capabilities, InputTooLong, Usage
from ..sse import parse_events


def _usage(u) -> Optional[Usage]:
    if not isinstance(u, dict):
        return None
    d = u.get("prompt_tokens_details") or {}
    return Usage(input_tokens=u.get("prompt_tokens"), cached_input_tokens=d.get("cached_tokens"),
                 output_tokens=u.get("completion_tokens"))


class OpenAICompatChat(CachedChatModel):
    adapter_id = "openai-compat"

    def __init__(self, base_url: str, model: str, token: Optional[str] = None, token_file: Optional[str] = None,
                 max_context_tokens: Optional[int] = None, operator_declares_not_subscription: bool = False, **kw):
        if token is None and token_file:
            with open(token_file) as fh:
                token = fh.read().strip()
        self.base_url, self._token = base_url.rstrip("/"), token
        # Taint by default (review N3): any OpenAI-compatible endpoint may front a subscription gateway, and host spellings
        # (localhost., 127.1, 0.0.0.0, [::ffff:127.0.0.1], *.nip.io, DNS) cannot be classified reliably. Only an explicit
        # operator declaration clears it, and even then the gateway refuses it off loopback (not an allow-listed stub type).
        self.subscription_backed = operator_declares_not_subscription is not True
        super().__init__(model=model, **kw)
        self.capabilities = Capabilities(adapter=self.adapter_id, supports_json_schema=True, json_schema_mode="prompt+validate",
                                         supports_system=True, reports_usage=True, max_context_tokens=max_context_tokens,
                                         supports_temperature=True, supports_max_output_tokens=True, streaming="native")

    def _body(self, prompt, system, json_schema, stream=False, max_output_tokens=None, temperature=None) -> dict:
        msgs = ([{"role": "system", "content": system}] if system is not None else []) + [{"role": "user", "content": prompt}]
        body = {"model": self.model, "messages": msgs, "stream": stream}
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": json_schema, "strict": True}}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _open(self, body: dict, timeout_s: float):
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + self._token} if self._token else {})})
        try:
            return _urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as e:
            raw = e.read()[:2000].decode("utf-8", "replace")
            try:
                code = (json.loads(raw).get("error") or {}).get("code")
            except ValueError:
                code = None
            if code == "context_length_exceeded":
                raise InputTooLong(raw[:200])
            raise BackendError("HTTP %d: %s" % (e.code, raw[:300]))
        except (socket.timeout, TimeoutError):
            raise CallTimeout("no response within %.1fs" % timeout_s)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise CallTimeout("no response within %.1fs" % timeout_s)
            raise BackendError("connection failed: %s" % e.reason)

    def _call_backend(self, prompt, system, json_schema, timeout_s, params=None) -> BackendOut:
        p = params or {}
        try:
            with self._open(self._body(prompt, system, json_schema, max_output_tokens=p.get("max_output_tokens"), temperature=p.get("temperature")), timeout_s) as r:
                d = json.loads(r.read())
        except (socket.timeout, TimeoutError):
            raise CallTimeout("no response within %.1fs" % timeout_s)
        ch = (d.get("choices") or [{}])[0]
        return BackendOut(text=(ch.get("message") or {}).get("content") or "", usage=_usage(d.get("usage")),
                          meta={"finish_reason": ch.get("finish_reason"), "upstream_model": d.get("model")})

    def stream(self, messages: Sequence[dict], system: Optional[str] = None, timeout_s: Optional[float] = None
               ) -> Iterator[Tuple[str, Optional[Usage]]]:
        """Yield (text_delta, None) per content chunk, then ('', usage) once if the server sent a usage chunk.
        Streams are not cached (nothing to replay) but ARE counted against this client's cap and ledgered."""
        prompt = render_messages(messages)
        if self.egress_guard is not None:                 # same egress guard as complete(), before the cap is spent
            self.egress_guard.check_text(prompt, "stream prompt")
            if system:
                self.egress_guard.check_text(system, "stream system")
        self._reserve()                                   # streams are counted, capped and ledgered like complete()
        self._ledger(kind="stream", status="started")
        body = self._body(prompt, system, None, stream=True)
        with self._open(body, timeout_s or self.timeout_s) as r:
            for ev in parse_events(r):
                if ev == "[DONE]":
                    return
                if ev.get("usage"):
                    yield "", _usage(ev["usage"])
                for c in ev.get("choices") or []:
                    t = (c.get("delta") or {}).get("content")
                    if t:
                        yield t, None
