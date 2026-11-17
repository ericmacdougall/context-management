"""The port tap. Wraps a structured model transport (provider-shaped request in, provider-shaped response or event stream out).

NOTE: memcore.ports.ChatModel is string-only ({role, content: str}); it cannot carry thinking blocks, tool_use or tool_result.
This transport interface is NEW, UNREVIEWED surface (open design question). Identity and tenant are values resolved by a trusted
caller behind `Identity`; the tap never reads them from the request.

Write order (the invariant): REQ record durable BEFORE the request is sent; RESP record durable BEFORE the response (or, for a
stream, the terminal event) is returned to the caller. Redaction and the egress guard run BEFORE either record is written.
"""
from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import shapes
from .canon import Hasher, cj
from .portlog import PortLog
from .scan import Scanner, SecretBlocked, redact_obj, redact_text


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
    itself handed out (never a marker pattern); a host that alters the block defeats the match (documented).
    Persistence: the (redacted) block text goes into the append-only content-addressed blob store in the same transaction as the REQ
    record that references it (inside the hash chain); there is no separate table. A block that contained a secret is stored
    redacted, so after a restart it no longer matches exactly (documented edge)."""

    def __init__(self, log: PortLog, stream: str, hasher: Hasher, scanner=None, red=None):
        self.log, self.stream, self.hasher, self.scanner, self.red = log, stream, hasher, scanner, red
        self.texts: Dict[str, str] = log.issued_blocks(stream)
        self.persisted = set(self.texts)

    def add(self, text: str) -> str:
        h = self.hasher.h(text)
        if h not in self.texts:
            self.texts[h] = text
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


def sha_keys(keys: List[str]) -> str:
    import hashlib
    return hashlib.sha256("|".join(keys).encode()).hexdigest()[:20]


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
        self._mcache: Dict[tuple, dict] = {}
        self._cursor: Dict[str, dict] = {}

    def flush(self) -> None:
        """The real tap has nothing pending: both halves are durable before the data is returned. LateTap overrides this."""

    # ------------------------------------------------------------ helpers
    def _stream_id(self) -> Tuple[str, str]:
        tenant, session = self.identity.resolve()
        if not tenant or not session or "/" in tenant:
            raise ValueError("identity must resolve to a non-empty tenant (no '/') and session")
        return "%s/%s" % (tenant, session), session

    def registry(self, stream: str) -> Registry:
        if stream not in self._reg:
            self._reg[stream] = Registry(self.log, stream, self.hasher, self.scanner, self._red)
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
        m = self._red(strip_media(msg), counter)
        text = cj(m)
        h = self.hasher.h(text)[:32]
        blobs[h] = text
        return {"h": h, "k": self.hasher.h(shapes.message_key(self.provider, m))[:20], "role": msg.get("role")}

    def _red(self, obj, counter):
        """The ONE redaction entry point (every write path goes through it; the scan-after-write control overrides it)."""
        return redact_obj(obj, self.scanner, self.hasher, counter)

    # ------------------------------------------------------------ request half
    def _msg_entry(self, stream: str, m: dict, reg: Registry) -> dict:
        """Per-message pipeline (guard, excision of registered blocks, redaction, hashing), cached by the message's canonical JSON so an
        unchanged message costs one serialisation and a lookup on later calls (cost must not grow with history)."""
        ck = (stream, cj(m))
        ent = self._mcache.get(ck)
        if ent is not None:
            return ent
        if self.guard is not None:
            self.guard.check(m, "model request message")
        _, mx, spans = excise_injected(self.provider, None, [m], reg)
        counter: list = []
        blobs: Dict[str, str] = {}
        ref = self._put_msg(mx[0], blobs, counter)
        ent = {"ref": ref, "blobs": blobs, "spans": spans, "red": len(counter), "persisted": False}
        self._mcache[ck] = ent
        return ent

    def _prepare(self, request: dict, stream_flag: bool):
        stream, session = self._stream_id()
        sysm, messages = shapes.split_request(self.provider, request)
        reg = self.registry(stream)
        cur = self._cursor.get(stream)
        p = 0
        if cur:
            pm = cur["msgs"]
            lim = min(len(pm), len(messages))
            while p < lim and messages[p] is pm[p]:
                p += 1
        keep = max(p - 8, 0)         # identity fast path for messages older than the last 8; the last 8 are re-verified by content
        try:
            if self.guard is not None:
                self.guard.check({k: v for k, v in request.items() if k != "messages"}, "model request")
            ents = cur["ents"][:keep] if cur else []
            new_ents = []
            for m in messages[keep:]:
                e = self._msg_entry(stream, m, reg)
                ents.append(e)
                if not e["persisted"]:
                    new_ents.append(e)
        except Exception as e:                      # egress guard refusal: fail closed, tombstone keeps the sequence contiguous
            if self.guard is not None and e.__class__.__name__ == "EgressLeak":
                self._append(stream, "BLOCK", None, {"reason": "egress_guard", "error": type(e).__name__}, new_call=True)
            raise
        injection_meta = None
        sent = dict(request)
        sent["messages"] = list(messages)
        if self.injector is not None:
            from memcore.ports import Turn
            inj = self.injector.build(Turn(session_id=session, user_text=last_user_text(self.provider, messages), time=self.clock()))
            if inj.block:
                ih = reg.add(inj.block)
                self._place(sent, inj.block)
                injection_meta = {"h": ih, "ids": list(inj.used_ids), "dropped": inj.dropped, "placed_by": "tap", "len": len(inj.block)}
        spans: List[dict] = []
        for mi in range(keep, len(ents)):
            spans += [dict(sp, msg=mi) for sp in ents[mi]["spans"]]
        if cur:
            spans = list(cur.get("spans_prefix", []))[:0] + spans
        sysx, sspans = sysm, []
        if reg.texts and sysm not in (None, "", []):
            sysx, _, sspans = excise_injected(self.provider, sysm, [], reg)
        spans += sspans
        if spans and injection_meta is None:
            injection_meta = {"h": spans[0]["h"], "ids": [], "placed_by": "host"}
        counter: list = [1] * sum(e["red"] for e in new_ents)
        blobs: Dict[str, str] = {}
        for e in new_ents:
            blobs.update(e["blobs"])
        mrefs = [e["ref"] for e in ents]
        sref = None
        if sysx not in (None, "", []):
            s = self._red(strip_media(sysx), counter)
            st = cj(s)
            sref = {"h": self.hasher.h(st)[:32], "k": self.hasher.h(st)[:20]}
            blobs[sref["h"]] = st
        used_blocks = {sp["h"] for sp in spans} | ({injection_meta["h"]} if injection_meta else set())
        for h in used_blocks:
            if h not in reg.persisted and h in reg.texts:
                blobs[h] = self._red(reg.texts[h], [])
        if injection_meta and injection_meta.get("placed_by") == "tap":
            if self.scanner.scan(reg.texts[injection_meta["h"]]):
                counter.append(1)
                injection_meta["scanner_findings"] = True
        if counter and self.secret_policy == "block":
            self._append(stream, "BLOCK", None, {"reason": "secret_in_request", "findings": len(counter)}, new_call=True)
            raise SecretBlocked("secret found in outbound request (%d findings)" % len(counter))
        tools = request.get("tools")
        delta, body_msgs = self._delta(stream, mrefs)
        body = {"req_id": uuid.uuid4().hex, "provider": self.provider, "model": request.get("model"), "stream": stream_flag,
                "msgs": body_msgs, "sys": sref, "tools_h": self.hasher.hj(tools) if tools else None,
                "tool_names": sorted((t.get("name") or t.get("function", {}).get("name") or "?") for t in tools) if tools else [],
                "injected": injection_meta, "spans": spans, "redactions": len(counter),
                "pairing": shapes.check_pairing(self.provider, messages[max(keep - 2, 0):])}
        if delta:
            body["delta"] = delta
        self._pending_keys = (stream, [m["k"] for m in mrefs])
        self._touched = (new_ents, used_blocks)
        self._cursor_next = (stream, {"msgs": list(messages), "ents": ents})
        return stream, sent, body, blobs, reg

    def _delta(self, stream: str, mrefs: List[dict]):
        """REQ stores only messages beyond the common prefix with the previous call (its request plus its logged response). Single
        writer per stream is assumed; a stale cache surfaces as a digest mismatch at ingest (typed gap), never silently."""
        last = getattr(self, "_last", {}).get(stream)
        keys = [m["k"] for m in mrefs]
        if not last:
            return None, mrefs
        full = last["keys"] + ([last["resp_k"]] if last["resp_k"] else [])
        p = 0
        while p < len(keys) and p < len(full) and keys[p] == full[p]:
            p += 1
        if p == 0:
            return None, mrefs
        return {"base": last["call"], "with_resp": bool(last["resp_k"]), "p": p, "digest": sha_keys(full[:p])}, mrefs[p:]

    def seed(self, messages: List[dict]) -> None:
        """Resumed session: record a SIGNED baseline of the history the host resumes with (keys only, never ingested as events)."""
        stream, _ = self._stream_id()
        reg = self.registry(stream)
        keys = [self._msg_entry(stream, m, reg)["ref"]["k"] for m in messages]
        digest = sha_keys(keys)
        self._append(stream, "SEED", None, {"keys": keys, "digest": digest, "sig": self.hasher.h("seed|%s|%s" % (stream, digest))}, new_call=True)

    def backfill(self, gap_kind: str, key: str, authorized_by: str, backfill_hasher: Hasher) -> None:
        """Authorised backfill record that clears a detected gap (e.g. a bypass); signed with a key the ingester trusts separately."""
        stream, _ = self._stream_id()
        sig = backfill_hasher.h("backfill|%s|%s|%s|%s" % (stream, gap_kind, key, authorized_by))
        self._append(stream, "BACKFILL", None, {"gap_kind": gap_kind, "key": key, "authorized_by": authorized_by, "sig": sig}, new_call=True)

    def recover_unconfirmed(self, recoverer) -> int:
        """Ask a provider-side recoverer for the response of every REQ with no RESP record. Interface only: as of 2026-11-16 neither
        Anthropic Messages nor OpenAI chat documents an idempotency key or retrieval of a synchronous response (NOT AVAILABLE)."""
        stream, _ = self._stream_id()
        reg = self.registry(stream)
        recs = self.log.read(stream)
        answered = {r["call_n"] for r in recs if r["kind"] == "RESP"}
        n = 0
        for r in recs:
            if r["kind"] != "REQ" or r["call_n"] in answered:
                continue
            body = json.loads(r["body"])
            resp = recoverer(body["req_id"], r["call_n"])
            if not resp:
                continue
            msg = shapes.response_message(self.provider, resp)
            stop, done = shapes.stop_info(self.provider, resp)
            self._resp_record(stream, r["call_n"], reg, "recovered", msg, stop, done, [], resp.get("id"), resp.get("usage"), resp.get("model"))
            n += 1
        return n

    def _place(self, sent: dict, block: str) -> None:
        """Place the injected block as its OWN block in the last user message (never merged into the user's text). tool_result blocks
        must stay first in a user message (Anthropic: text after tool_results), so the block goes after any leading tool_results."""
        msgs = sent["messages"]
        for idx in range(len(msgs) - 1, -1, -1):
            m = msgs[idx]
            if m.get("role") == "user":
                c = m.get("content")
                parts = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
                i = 0
                while i < len(parts) and isinstance(parts[i], dict) and parts[i].get("type") == "tool_result":
                    i += 1
                m2 = dict(m)
                m2["content"] = parts[:i] + [{"type": "text", "text": block}] + parts[i:]
                msgs[idx] = m2
                return
        msgs.append({"role": "user", "content": [{"type": "text", "text": block}]})

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
        if mref is not None and getattr(self, "_last", {}).get(stream, {}).get("call") == call_n:
            self._last[stream]["resp_k"] = mref["k"]

    def call(self, request: dict, stream: bool = False, retry_of: Optional[int] = None, new_count: Optional[int] = None):
        """retry_of: OPTIONAL host-declared retry identity (the call number being resent). Content alone cannot tell a retry from a
        legitimate repeat; without this the ingester treats an identical resend as a flagged possible duplicate. The tap records the claim;
        the ingester honours it only for the immediately preceding confirmed call with identical keys (ASSUMPTION: an honest host).
        new_count: OPTIONAL explicit boundary, the number of trailing messages of this request that are new. When given the ingester uses it
        and never infers; without it, alignment that is not provably unambiguous opens a blocking gap (ASSUMPTION: an honest, correct host)."""
        stream_id, sent, body, blobs, reg = self._prepare(request, stream)
        if retry_of is not None:
            body["retry_of"] = int(retry_of)
        if new_count is not None:
            body["new_count"] = new_count          # validated by the ingester (integer, 0 to the request length); see PREREG-amend-4 A1
        self.crash("before_req_log")
        seq, call_n, _ = self._append(stream_id, "REQ", None, body, blobs, new_call=True)
        new_ents, used_blocks = self._touched
        for e in new_ents:
            e["persisted"] = True
        self._cursor[self._cursor_next[0]] = self._cursor_next[1]
        reg.persisted |= used_blocks
        if not hasattr(self, "_last"):
            self._last = {}
        self._last[stream_id] = {"call": call_n, "keys": self._pending_keys[1], "resp_k": None}
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
        except Rejected as e:
            if not wrote:
                wrote = True
                self._resp_record(stream_id, call_n, reg, "rejected", None, None, False, [], None, None, None, type(e).__name__)
            raise
        except Exception as e:
            write("transport_error:%s" % type(e).__name__)
            raise
