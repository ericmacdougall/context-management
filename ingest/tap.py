"""The port tap. Wraps a structured model transport (provider-shaped request in, provider-shaped response or event stream out).

NOTE: memcore.ports.ChatModel is string-only ({role, content: str}); it cannot carry thinking blocks, tool_use or tool_result.
This transport interface is NEW, UNREVIEWED surface (open design question). Identity and tenant are values resolved by a trusted
caller behind `Identity`; the tap never reads them from the request.

Write order (the invariant): REQ record durable BEFORE the request is sent; RESP record durable BEFORE the response (or, for a
stream, the terminal event) is returned to the caller. Redaction and the egress guard run BEFORE either record is written.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import shapes
from .canon import Hasher, cj
from .portlog import PortLog
from .scan import Scanner, SecretBlocked, redact_obj


class Identity:
    """Trusted-caller interface: (tenant, session) for the call in progress."""

    def resolve(self) -> Tuple[str, str]:
        raise NotImplementedError


class StaticIdentity(Identity):
    def __init__(self, tenant: str, session: str):
        self.tenant, self.session = tenant, session

    def resolve(self):
        return self.tenant, self.session


class Rejected(Exception):
    """Transport signals the provider refused the request (HTTP 4xx): the model did not process it."""


class SimulatedCrash(BaseException):
    pass


class CrashPlan:
    """Named crash hook (proof rule 10): fires at `point` (optionally the idx-th event) by calling `action`."""

    def __init__(self, point: str, action: Callable[[], None], at: Optional[int] = None):
        self.point, self.action, self.at = point, action, at

    def hit(self, point: str, idx: Optional[int] = None) -> None:
        if point == self.point and (self.at is None or idx == self.at):
            self.action()


def _noop_crash(point: str, idx: Optional[int] = None) -> None:
    return None


class Registry:
    """Blocks the injector issued, by keyed hash. Provenance 'memory_injected' comes from an EXACT match against a value the port
    itself handed out (never a marker pattern); a host that alters the block defeats the match (documented)."""

    def __init__(self, log: PortLog, stream: str, hasher: Hasher):
        self.log, self.stream, self.hasher = log, stream, hasher
        self.texts: Dict[str, str] = log.issued(stream)

    def add(self, text: str) -> str:
        h = self.hasher.h(text)
        if h not in self.texts:
            self.texts[h] = text
            self.log.record_issued(self.stream, h, text)
        return h


def _excise_str(s: str, reg: Registry) -> Tuple[str, List[Tuple[int, str]]]:
    spans = []
    for h, t in sorted(reg.texts.items(), key=lambda kv: -len(kv[1])):
        if not t:
            continue
        while True:
            i = s.find(t)
            if i < 0:
                break
            spans.append((i, h))
            s = s[:i] + s[i + len(t):]
    return s, spans


def excise_injected(provider: str, system: Any, messages: List[dict], reg: Registry) -> Tuple[Any, List[dict], List[dict]]:
    """Remove registry blocks from user-role text and from the system prompt. Returns (system, messages, spans) where spans give
    the structural position of each removal (msg index or -1 for system, block index, offset, hash)."""
    spans: List[dict] = []
    system = copy.deepcopy(system)
    msgs = copy.deepcopy(messages)
    if not reg.texts:
        return system, msgs, spans
    if isinstance(system, str):
        system, sp = _excise_str(system, reg)
        spans += [{"msg": -1, "blk": 0, "off": o, "h": h} for o, h in sp]
    elif isinstance(system, list):
        for bi, b in enumerate(system):
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                b["text"], sp = _excise_str(b["text"], reg)
                spans += [{"msg": -1, "blk": bi, "off": o, "h": h} for o, h in sp]
    for mi, m in enumerate(msgs):
        if m.get("role") not in ("user", "system", "developer"):
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"], sp = _excise_str(c, reg)
            spans += [{"msg": mi, "blk": 0, "off": o, "h": h} for o, h in sp]
        elif isinstance(c, list):
            for bi, b in enumerate(c):
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                    b["text"], sp = _excise_str(b["text"], reg)
                    spans += [{"msg": mi, "blk": bi, "off": o, "h": h} for o, h in sp]
    return system, msgs, spans


def strip_media(obj: Any) -> Any:
    """Media bytes are not stored in the log: replaced by a length marker (the keyed hash would be computed over the marker)."""
    if isinstance(obj, dict):
        if obj.get("type") in ("image", "document") and isinstance(obj.get("source"), dict) and "data" in obj["source"]:
            o = dict(obj)
            s = dict(o["source"])
            s["data"] = "[media omitted: %d chars]" % len(str(s["data"]))
            o["source"] = s
            return o
        return {k: strip_media(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_media(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("data:") and len(obj) > 256:
        return "[media omitted: %d chars]" % len(obj)
    return obj


def last_user_text(provider: str, messages: List[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return " ".join(b.data.get("text", "") for b in shapes.blocks_of_message(provider, m)
                            if b.prov == "user" and b.kind == "text" and isinstance(b.data, dict))
    return ""


class PortTap:
    def __init__(self, transport, provider: str, log: PortLog, identity: Identity, hasher: Hasher, scanner: Scanner,
                 guard=None, injector=None, secret_policy: str = "redact_log", crash=None, witness: Optional[Callable] = None,
                 stream_delivery: str = "live", clock: Callable[[], str] = lambda: "2026-01-01T00:00:00Z"):
        assert provider in (shapes.ANTHROPIC, shapes.OPENAI)
        assert secret_policy in ("redact_log", "block")
        assert stream_delivery in ("live", "buffered")
        self.t, self.provider, self.log, self.identity = transport, provider, log, identity
        self.hasher, self.scanner, self.guard, self.injector = hasher, scanner, guard, injector
        self.secret_policy, self.witness, self.stream_delivery, self.clock = secret_policy, witness, stream_delivery, clock
        self.crash = crash.hit if isinstance(crash, CrashPlan) else (crash or _noop_crash)
        self._reg: Dict[str, Registry] = {}

    # ------------------------------------------------------------ helpers
    def _stream_id(self) -> Tuple[str, str]:
        tenant, session = self.identity.resolve()
        if not tenant or not session or "/" in tenant:
            raise ValueError("identity must resolve to a non-empty tenant (no '/') and session")
        return "%s/%s" % (tenant, session), session

    def registry(self, stream: str) -> Registry:
        if stream not in self._reg:
            self._reg[stream] = Registry(self.log, stream, self.hasher)
        return self._reg[stream]

    def register_issued(self, injection) -> str:
        """For hosts that call the injector themselves and merge the block into their own message: tell the port the block."""
        stream, _ = self._stream_id()
        return self.registry(stream).add(injection.block)

    def _append(self, stream: str, kind: str, call_n, body: dict, blobs=None, new_call=False):
        r = self.log.append(stream, kind, call_n, body, blobs, new_call)
        if self.witness:
            self.witness(stream, r[0], r[2])
        return r

    def _put_msg(self, msg: dict, blobs: Dict[str, str], counter: list) -> dict:
        m = redact_obj(strip_media(msg), self.scanner, self.hasher, counter)
        text = cj(m)
        h = self.hasher.h(text)
        blobs[h] = text
        return {"h": h, "k": self.hasher.h(shapes.message_key(self.provider, m)), "role": msg.get("role")}

    # ------------------------------------------------------------ request half
    def _prepare(self, request: dict, stream_flag: bool):
        stream, session = self._stream_id()
        if self.guard is not None:
            try:
                self.guard.check(request, "model request")
            except Exception as e:                      # egress guard refusal: fail closed, tombstone keeps the sequence contiguous
                self._append(stream, "BLOCK", None, {"reason": "egress_guard", "error": type(e).__name__}, new_call=True)
                raise
        sysm, messages = shapes.split_request(self.provider, request)
        reg = self.registry(stream)
        injection_meta = None
        sent = copy.deepcopy(request)
        if self.injector is not None:
            from memcore.ports import Turn
            inj = self.injector.build(Turn(session_id=session, user_text=last_user_text(self.provider, messages), time=self.clock()))
            if inj.block:
                ih = reg.add(inj.block)
                self._place(sent, inj.block)
                injection_meta = {"h": ih, "ids": list(inj.used_ids), "dropped": inj.dropped, "placed_by": "tap", "len": len(inj.block)}
        # structural excision from the ORIGINAL request (host-merged blocks); the tap's own placement is not in `request`
        sysx, msgsx, spans = excise_injected(self.provider, sysm, messages, reg)
        if spans and injection_meta is None:
            injection_meta = {"h": spans[0]["h"], "ids": [], "placed_by": "host"}
        counter: list = []
        blobs: Dict[str, str] = {}
        mrefs = [self._put_msg(m, blobs, counter) for m in msgsx]
        sref = None
        if sysx not in (None, "", []):
            s = redact_obj(strip_media(sysx), self.scanner, self.hasher, counter)
            st = cj(s)
            sref = {"h": self.hasher.h(st), "k": self.hasher.h(st)}
            blobs[sref["h"]] = st
        if injection_meta and injection_meta.get("placed_by") == "tap":
            if self.scanner.scan(reg.texts[injection_meta["h"]]):
                counter.append(1)
                injection_meta["scanner_findings"] = True
        if counter and self.secret_policy == "block":
            self._append(stream, "BLOCK", None, {"reason": "secret_in_request", "findings": len(counter)}, new_call=True)
            raise SecretBlocked("secret found in outbound request (%d findings)" % len(counter))
        tools = request.get("tools")
        body = {"req_id": uuid.uuid4().hex, "provider": self.provider, "model": request.get("model"), "stream": stream_flag,
                "msgs": mrefs, "sys": sref, "tools_h": self.hasher.hj(tools) if tools else None,
                "tool_names": sorted((t.get("name") or t.get("function", {}).get("name") or "?") for t in tools) if tools else [],
                "injected": injection_meta, "spans": spans, "redactions": len(counter),
                "pairing": shapes.check_pairing(self.provider, messages)}
        return stream, sent, body, blobs, reg

    def _place(self, sent: dict, block: str) -> None:
        """Place the injected block as its OWN block at the front of the last user message (never merged into the user's text)."""
        msgs = sent.get("messages") or []
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                parts = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
                m["content"] = [{"type": "text", "text": block}] + parts
                return
        msgs.append({"role": "user", "content": [{"type": "text", "text": block}]})
        sent["messages"] = msgs

    # ------------------------------------------------------------ response half
    def _resp_record(self, stream, call_n, reg, status, msg, stop, turn_complete, bad, resp_id, usage, model, err=None):
        blobs: Dict[str, str] = {}
        counter: list = []
        mref = self._put_msg(msg, blobs, counter) if msg is not None else None
        quotes = []
        if msg is not None:
            for bi, b in enumerate(shapes.blocks_of_message(self.provider, msg)):
                if b.prov == "assistant" and b.kind == "text" and isinstance(b.data, dict):
                    t = b.data.get("text", "")
                    for h, bt in reg.texts.items():
                        if bt and bt in t:
                            quotes.append({"blk": bi, "h": h})
        body = {"status": status, "stop": stop, "turn_complete": turn_complete, "bad_blocks": bad, "resp_id": resp_id,
                "usage": usage, "model": model, "msg": mref, "quotes_injected": quotes, "redactions": len(counter), "error": err}
        self._append(stream, "RESP", call_n, body, blobs)

    def call(self, request: dict, stream: bool = False):
        stream_id, sent, body, blobs, reg = self._prepare(request, stream)
        self.crash("before_req_log")
        seq, call_n, _ = self._append(stream_id, "REQ", None, body, blobs, new_call=True)
        self.crash("after_req_log_before_send")
        if stream:
            return self._stream(stream_id, call_n, reg, sent)
        try:
            resp = self.t.call(sent)
        except Rejected as e:
            self._resp_record(stream_id, call_n, reg, "rejected", None, None, False, [], None, None, None, type(e).__name__)
            raise
        except Exception as e:
            self._resp_record(stream_id, call_n, reg, "failed_unknown", None, None, False, [], None, None, None, type(e).__name__)
            raise
        self.crash("after_send_before_resp_log")
        msg = shapes.response_message(self.provider, resp)
        stop, done = shapes.stop_info(self.provider, resp)
        self._resp_record(stream_id, call_n, reg, "complete" if done else "turn_incomplete", msg, stop, done, [],
                          resp.get("id"), resp.get("usage"), resp.get("model"))
        self.crash("after_resp_log_before_return")
        return resp

    def _stream(self, stream_id, call_n, reg, sent) -> Iterator[Any]:
        acc = shapes.AnthropicAcc() if self.provider == shapes.ANTHROPIC else shapes.OpenAIAcc()
        terminal = (lambda ev: isinstance(ev, dict) and ev.get("type") == "message_stop") if self.provider == shapes.ANTHROPIC \
            else (lambda ev: ev == "[DONE]")
        wrote = False
        held: List[Any] = []

        def write(status_if_incomplete: str, err=None):
            nonlocal wrote
            if wrote:
                return
            wrote = True
            if self.provider == shapes.ANTHROPIC:
                msg, bad = acc.result()
                complete = acc.stop_seen and acc.error is None
                stop = msg.get("stop_reason")
                rid, usage, model = msg.get("id"), msg.get("usage"), msg.get("model")
                msg = {"role": "assistant", "content": msg["content"]}
            else:
                resp, bad = acc.result()
                complete = acc.finish is not None and acc.error is None
                msg = resp["choices"][0]["message"]
                stop, rid, usage, model = acc.finish, resp["id"], None, None
            if complete and not bad:
                done = stop in shapes.COMPLETE_STOPS[self.provider]
                self._resp_record(stream_id, call_n, reg, "complete" if done else "turn_incomplete", msg, stop, done, [], rid, usage, model)
            else:
                self._resp_record(stream_id, call_n, reg, "incomplete" if not complete else "turn_incomplete", msg, stop, False, bad,
                                  rid, usage, model, err or ("block_incomplete" if complete else status_if_incomplete))

        idx = 0
        try:
            for ev in self.t.stream(sent):
                self.crash("mid_stream", idx)
                idx += 1
                acc.feed(ev)
                err = getattr(acc, "error", None)
                if err is not None:
                    write("provider_error", "stream_error:%s" % (err.get("type") if isinstance(err, dict) else "?"))
                    raise shapes.StreamError("provider error event")
                if terminal(ev):
                    write("n/a")
                    self.crash("after_resp_log_before_return")
                    if self.stream_delivery == "buffered":
                        for h in held:
                            yield h
                    yield ev
                    return
                if self.stream_delivery == "buffered":
                    held.append(ev)
                else:
                    yield ev
            write("stream_ended_without_terminal")
            raise shapes.StreamError("stream ended without terminal event")
        except GeneratorExit:
            write("caller_abandoned")
            raise
        except shapes.StreamError:
            raise
        except Exception as e:
            write("transport_error:%s" % type(e).__name__)
            raise
