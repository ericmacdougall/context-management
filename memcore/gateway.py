"""Local OpenAI-compatible gateway in front of any ChatModel adapter (claude-cli, codex-cli, ...).

SINGLE-USER, LOCAL, DEVELOPMENT ONLY. Binds 127.0.0.1 and refuses anything else; a random bearer token is written to a
0600 file; the backend adapter's call cap, cache and ledger apply to every request. Streaming is SIMULATED: the backend
call completes first, then the text is framed as SSE chunks with a final usage chunk.
Start:  python3 -m memcore.gateway --backend claude-cli --out <dir>      (writes <dir>/gateway.{token,port,pid})
Stop:   touch <dir>/gateway.stop     (the server polls for it and shuts down cleanly)
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import Callable, Optional

from .budget import CallBudget
from .ports import (BackendError, CallCapExceeded, CallTimeout, ChatModel, InputTooLong, PortError, SchemaError)
from .sse import chat_chunks

MAX_BODY = 8 * 1024 * 1024
BRIDGE_ADDRESSES = {"172.17.0.1"}   # the one Docker bridge address a container client may reach; never configurable per call
BANNER = ("=" * 72 + "\nmemcore gateway: SINGLE-USER, LOCAL, DEVELOPMENT ONLY. Bound to %s:%d.\n"
          "Backed by a subscription CLI; every request spends the backend call cap. Do not expose.\n" + "=" * 72)


def require_loopback(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False  # hostnames (even 'localhost') are refused: no resolver surprises
    if not ok:
        raise ValueError("refusing non-loopback bind %r; only 127.0.0.1 / ::1 are allowed" % host)


def _network_safe(b) -> bool:
    """Default deny. True only for an instance of EXACTLY an allow-listed data-only stub type (no subclass, no wrapper, no
    attribute a caller can set), with no callable injected on the instance, no shadowed method, its own guard and budget
    types, and string-only rules. A flag the object declares about itself is never trusted."""
    from .budget import CallBudget
    from .fakes import ScriptedStubChat
    from .guard import EgressGuard
    if type(b) is not ScriptedStubChat:
        return False
    inst = vars(b)
    cls_methods = {k for k in dir(type(b)) if callable(getattr(type(b), k, None))}
    if any(callable(v) for v in inst.values()) or cls_methods & set(inst):
        return False
    if getattr(b, "subscription_backed", False) or type(b.egress_guard) is not EgressGuard or type(b.budget) is not CallBudget:
        return False
    rules = inst.get("rules")
    return (isinstance(rules, tuple) and all(type(r) is tuple and len(r) == 2 and all(type(x) is str for x in r) for r in rules)
            and type(inst.get("default")) is str)


def check_gate(backs: Mapping, host: str, allow_bridge_host: Optional[str], dev_subscription_backends: bool) -> None:
    """The bind/backend gate. Run at construction over the snapshot the handler will use, and again per request."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    tainted = [m for m, b in backs.items() if getattr(b, "subscription_backed", False)]
    if tainted:   # subscription CLIs, or any client/wrapper that declares the taint: explicit dev flag, loopback only
        from .devgate import require_dev_subscription
        require_dev_subscription(dev_subscription_backends, "Gateway over subscription-backed backend(s) %s" % tainted)
    if loopback:
        require_loopback(host)
        return
    # FAIL CLOSED: a non-loopback bind needs the one named bridge address AND every backend to be an exact allow-listed
    # data-only stub (_network_safe). FakeChat (caller code), subclasses, wrappers, routers and clients are refused.
    if allow_bridge_host != host or host not in BRIDGE_ADDRESSES:
        raise ValueError("refusing non-loopback bind %r: only loopback or the named bridge address %s" % (host, sorted(BRIDGE_ADDRESSES)))
    unsafe = [m for m, b in backs.items() if not _network_safe(b)]
    if unsafe:
        raise ValueError("refusing non-loopback bind: backend(s) %s are not an allow-listed data-only stub type (ScriptedStubChat); "
                         "anything else may front a subscription and is refused off loopback" % unsafe)


def to_port_request(body: dict) -> dict:
    """OpenAI chat request -> ChatModel.complete kwargs (+ "_tool": name of a forced function, if any).
    Accepts and ignores n, user, reasoning_effort, stream_options details, max_completion_tokens (mapped).
    Structured output over the Elastic route = ONE forced function tool (tool_choice naming it): its parameters become the
    json_schema. Anything else about tools is refused."""
    msgs, system = [], []
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):  # content parts: text only
            if any(p.get("type") != "text" for p in content):
                raise ValueError("only text content parts are supported")
            content = "".join(p.get("text", "") for p in content)
        if content is None and role == "assistant":
            content = ""
        if not isinstance(content, str):
            raise ValueError("message content must be a string")
        if role in ("system", "developer"):
            system.append(content)
        elif role in ("user", "assistant"):
            msgs.append({"role": role, "content": content})
        else:
            raise ValueError("unsupported message role %r" % (role,))
    if not msgs:
        raise ValueError("no user message")
    schema, tool = None, None
    tools = body.get("tools") or []
    if tools:
        fn = (tools[0] or {}).get("function") or {}
        tc = body.get("tool_choice")
        forced = isinstance(tc, dict) and (tc.get("function") or {}).get("name") == fn.get("name")
        if len(tools) != 1 or not forced or not isinstance(fn.get("parameters"), dict):
            raise ValueError("only a single forced function tool (tool_choice naming it) is supported")
        schema, tool = fn["parameters"], fn["name"]
    rf = body.get("response_format") or {}
    if schema is None and rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise ValueError("response_format.json_schema.schema missing")
    elif schema is None and rf.get("type") == "json_object":
        schema = {"type": "object"}
    return {"messages": msgs, "system": "\n\n".join(system) if system else None, "json_schema": schema,
            "max_output_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
            "temperature": body.get("temperature"), "_tool": tool}


def _b64_floats(vec) -> str:
    import base64
    import struct
    return base64.b64encode(struct.pack("<%df" % len(vec), *vec)).decode()


def make_handler(backends, token: str, embedder=None, default_model: Optional[str] = None, recorder: Optional[list] = None,
                 embed_budget=None, embed_kind: str = "passage", gate: Optional[Callable[[Mapping], None]] = None,
                 pre_exec_injector=None, pre_exec_budget_s: float = 20.0, pre_exec_max_chars: Optional[int] = None):
    """backends: a ChatModel or a mapping; it is COPIED into a read-only mapping, so a caller mutating its dict afterwards
    changes nothing the handler serves. gate: re-run per request on the chosen backend (refuses with 503).
    pre_exec_injector: serve POST /v1/pre_exec (Kibana pre-execution workflow) with this BudgetedInjector; the same bearer
    token and per-request gate apply, and the reply always carries a new_prompt (the original prompt on any failure)."""
    if not isinstance(backends, Mapping):
        backends = {backends.model: backends}
    backends = MappingProxyType(dict(backends))
    if not backends and pre_exec_injector is None:
        raise ValueError("no backend and no pre_exec injector: nothing to serve")
    default_model = default_model or next(iter(backends), None)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # no request logging: bodies are private
            pass

        def _send(self, code, obj, headers=None):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _err(self, code, msg, ecode=None, headers=None):
            self._send(code, {"error": {"message": msg, "type": "invalid_request_error" if code < 500 else "server_error", "code": ecode}}, headers)

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                return True
            self._err(401, "invalid or missing bearer token", "invalid_api_key")
            return False

        def do_GET(self):
            if not self._auth():
                return
            if self.path.rstrip("/") == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "memcore-gateway"} for m in backends]})
            else:
                self._err(404, "not found")

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            self._raw_n = n
            if n <= 0 or n > MAX_BODY:
                self._err(413 if n > MAX_BODY else 400, "bad body length")
                return None
            try:
                b = json.loads(self.rfile.read(n))
            except (ValueError, RecursionError) as e:
                self._err(400, str(e))
                return None
            if recorder is not None:  # fixtures: path, non-secret headers and the parsed body (the bearer token is never recorded)
                recorder.append({"path": self.path, "headers": {k: v for k, v in self.headers.items() if k.lower() != "authorization"}, "body": b})
            return b

        def do_POST(self):
            if not self._auth():
                return
            path = self.path.rstrip("/")
            if path == "/v1/embeddings":
                return self._embeddings()
            if path == "/v1/pre_exec":
                return self._pre_exec()
            if path != "/v1/chat/completions":
                return self._err(404, "not found")
            body = self._body()
            if body is None:
                return
            try:
                kw = to_port_request(body)
            except (ValueError, AttributeError) as e:
                return self._err(400, str(e))
            tool = kw.pop("_tool")
            model = body.get("model") or default_model
            backend = backends.get(model)
            if backend is None:
                return self._err(404, "model %r not served here; have %s" % (model, sorted(backends)), "model_not_found")
            if gate is not None:
                try:
                    gate({model: backend})      # a backend mutated after construction (taint set, method injected) is refused
                except (ValueError, PortError) as e:
                    return self._err(503, "backend %r no longer passes the gateway gate: %s" % (model, type(e).__name__), "backend_refused")
            try:
                res = backend.complete(**kw)
            except InputTooLong as e:
                return self._err(400, str(e), "context_length_exceeded")
            except CallCapExceeded as e:
                return self._err(429, str(e), "call_cap_exceeded", {"Retry-After": "3600"})  # fast: never queue past the caller's timeout
            except CallTimeout as e:
                return self._err(504, str(e))
            except SchemaError as e:
                return self._err(502, str(e), "schema_validation_failed")
            except (BackendError, PortError) as e:
                return self._err(502, str(e))
            except Exception as e:  # noqa: BLE001 - any other backend failure is a clean 502, never a dropped connection
                return self._err(502, "backend failed: %s" % type(e).__name__, "backend_error")
            u = res.usage
            usage = None
            if u is not None and u.input_tokens is not None and u.output_tokens is not None:
                usage = {"prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens, "total_tokens": u.input_tokens + u.output_tokens}
                if u.cached_input_tokens is not None:
                    usage["prompt_tokens_details"] = {"cached_tokens": u.cached_input_tokens}
            want_usage = (body.get("stream_options") or {}).get("include_usage")
            usage_known = usage is not None
            if usage is None and body.get("stream") and want_usage:
                # The wire format requires all three counts in the final chunk, and the backend reported none: send zeros but say
                # so (x_memcore_usage_known false); non-stream replies simply omit usage.
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "x_memcore_usage_known": False}
            cid, created = "chatcmpl-" + res.request_hash[:24], int(time.time())
            call = {"id": "call_" + res.request_hash[:16], "name": tool, "arguments": res.text if res.parsed is None else json.dumps(res.parsed)} if tool else None
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in chat_chunks(cid, model, created, res.text, usage if want_usage else None, tool_call=call):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
            else:
                msg = {"role": "assistant", "content": "" if call else res.text}
                if call:
                    msg["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": tool, "arguments": call["arguments"]}}]
                self._send(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                                 "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                                 **({"usage": usage} if usage else {}), "x_memcore": {"cached": res.cached, "request_hash": res.request_hash}})

        def _pre_exec(self):
            if pre_exec_injector is None:
                return self._err(404, "no pre_exec injector configured")
            body = self._body()
            if body is None:
                return
            from .hosts.kibana_pre_exec import DEFAULT_BUDGET_S, parse_pre_exec_body, pre_exec_new_prompt
            from .hosts.prompt_hook import DEFAULT_MAX
            try:
                payload = parse_pre_exec_body(body)
            except ValueError as e:
                return self._err(400, str(e))
            if gate is not None:          # the same gate as the chat route, over everything this server fronts
                try:
                    gate(backends)
                except (ValueError, PortError) as e:
                    return self._err(503, "gateway no longer passes its gate: %s" % type(e).__name__, "backend_refused")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            out = pre_exec_new_prompt(pre_exec_injector, payload, now, budget_s=pre_exec_budget_s or DEFAULT_BUDGET_S,
                                      max_chars=pre_exec_max_chars or DEFAULT_MAX)
            self._send(200, out)

        def _embeddings(self):
            if embedder is None:
                return self._err(404, "no embedder configured")
            body = self._body()
            if body is None:
                return
            inp = body.get("input")
            texts = [inp] if isinstance(inp, str) else inp
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                return self._err(400, "input must be a string or a list of strings")
            # NOTE: the openai service never sends input_type; without one the gateway's configured embed_kind applies (default passage).
            it = body.get("input_type")
            kind = {"search": "query", "query": "query", "ingest": "passage", "passage": "passage"}.get(it, embed_kind) if it else embed_kind
            if embed_budget is not None:
                try:
                    embed_budget.reserve()
                except CallCapExceeded as e:
                    return self._err(429, str(e), "call_cap_exceeded", {"Retry-After": "3600"})
                embed_budget.ledger(kind="embed", n=len(texts), embed_kind=kind)
            try:
                r = embedder.embed(texts, kind=kind)
            except Exception as e:  # noqa: BLE001 - an embedder failure is a clean 502 (the reserved call stays counted)
                return self._err(502, "embedder failed: %s" % type(e).__name__, "backend_error")
            fmt = body.get("encoding_format", "float")
            data = [{"object": "embedding", "index": i, "embedding": _b64_floats(v) if fmt == "base64" else v} for i, v in enumerate(r.vectors)]
            n = sum(len(t.split()) for t in texts)
            self._send(200, {"object": "list", "data": data, "model": body.get("model") or r.model, "usage": {"prompt_tokens": n, "total_tokens": n}})
    return H


class Gateway:
    def __init__(self, backend, out_dir: str, host: str = "127.0.0.1", port: int = 0, embedder=None, recorder: Optional[list] = None,
                 dev_subscription_backends: bool = False, allow_bridge_host: Optional[str] = None, embed_cap: int = 1000,
                 embed_kind: str = "passage", pre_exec_injector=None, pre_exec_budget_s: float = 20.0,
                 pre_exec_max_chars: Optional[int] = None):
        """backend: a ChatModel or {model_id: ChatModel}. allow_bridge_host: one explicitly named private address (e.g. the Docker
        bridge gateway) a containerised client must reach; never 0.0.0.0, and the bearer token still applies."""
        # Snapshot FIRST, gate the snapshot, and hand the handler a read-only view of exactly what was gated (review N2).
        backs = MappingProxyType(dict(backend) if isinstance(backend, Mapping) else {backend.model: backend})
        if pre_exec_injector is not None:
            from .memtools import BudgetedInjector
            if type(pre_exec_injector) is not BudgetedInjector:   # the escaped, guarded, budgeted renderer and nothing else
                raise ValueError("pre_exec_injector must be exactly a memtools.BudgetedInjector")
        check_gate(backs, host, allow_bridge_host, dev_subscription_backends)
        gate = lambda sub: check_gate(sub, host, allow_bridge_host, dev_subscription_backends)
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir, self.token = out_dir, secrets.token_urlsafe(32)
        self.srv = ThreadingHTTPServer((host, port), make_handler(backs, self.token, embedder, recorder=recorder, gate=gate, embed_budget=CallBudget(out_dir, embed_cap, os.path.join(out_dir, "embed-ledger.jsonl"), name="embed"), embed_kind=embed_kind,
                                                                    pre_exec_injector=pre_exec_injector, pre_exec_budget_s=pre_exec_budget_s,
                                                                    pre_exec_max_chars=pre_exec_max_chars))
        self.srv.daemon_threads = True
        self.host, self.port = self.srv.server_address[:2]
        self._write("gateway.token", self.token, 0o600)
        self._write("gateway.port", str(self.port), 0o600)
        self._write("gateway.pid", str(os.getpid()), 0o600)
        self.stop_file = os.path.join(out_dir, "gateway.stop")
        if os.path.exists(self.stop_file):
            os.unlink(self.stop_file)

    def _write(self, name, text, mode):
        p = os.path.join(self.out_dir, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)

    @property
    def base_url(self):
        return "http://%s:%d/v1" % (self.host, self.port)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        threading.Thread(target=self._watch_stop, daemon=True).start()
        return t

    def _watch_stop(self):
        while not os.path.exists(self.stop_file):
            time.sleep(0.5)
        self.shutdown()

    def shutdown(self):
        self.srv.shutdown()
        self.srv.server_close()
        try:
            os.unlink(os.path.join(self.out_dir, "gateway.token"))  # a stale token must not outlive the server
        except FileNotFoundError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["claude-cli", "codex-cli"], required=True)
    ap.add_argument("--out", required=True, help="memcore-out directory (cache, ledger, token and port files)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--cap", type=int, default=200)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--dev-subscription-backends", action="store_true", help="REQUIRED: acknowledge development-only subscription use")
    a = ap.parse_args(argv)
    require_loopback(a.host)
    cache, ledger = os.path.join(a.out, "cache", a.backend), os.path.join(a.out, "ledger.jsonl")
    if a.backend == "claude-cli":
        from .adapters.claude_cli import ClaudeCliChat
        backend = ClaudeCliChat(dev_subscription_backends=a.dev_subscription_backends, cwd=os.path.join(a.out, "neutral"), model=a.model or "sonnet", cache_dir=cache, ledger_path=ledger, cap=a.cap)
    else:
        from .adapters.codex_cli import CodexCliChat
        backend = CodexCliChat(dev_subscription_backends=a.dev_subscription_backends, cwd=os.path.join(a.out, "neutral"), model=a.model, cache_dir=cache, ledger_path=ledger, cap=a.cap)
    gw = Gateway(backend, a.out, a.host, a.port, dev_subscription_backends=a.dev_subscription_backends)
    print(BANNER % (gw.host, gw.port), file=sys.stderr)
    print("base_url=%s token_file=%s stop_file=%s" % (gw.base_url, os.path.join(a.out, "gateway.token"), gw.stop_file), file=sys.stderr)
    gw.start()
    while os.path.exists(gw.stop_file) is False:
        time.sleep(0.5)
    time.sleep(1.0)


if __name__ == "__main__":
    main()
