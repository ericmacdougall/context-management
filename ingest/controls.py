"""Broken variants used as controls (proof rule 1). Each is the same harness with ONE property removed."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import shapes
from .canon import cj, sha
from .ingester import Ingester
from .portlog import GENESIS, connect
from .tap import PortTap, Rejected


class LateTap(PortTap):
    """Taps AFTER the response is returned to the caller (the host calls flush() once it has the answer). A crash between return
    and flush loses a turn the model completed and the host already received; a crash earlier loses the request too."""

    _pending = None

    def call(self, request: dict, stream: bool = False):
        stream_id, sent, body, blobs, reg = self._prepare(request, stream)
        self.crash("before_req_log")
        self.crash("after_req_log_before_send")          # nothing was written
        if not stream:
            resp = self.t.call(sent)
            self.crash("after_send_before_resp_log")
            msg = shapes.response_message(self.provider, resp)
            stop, done = shapes.stop_info(self.provider, resp)
            self._pending = (stream_id, body, blobs, reg, "complete" if done else "turn_incomplete", msg, stop, done, resp.get("id"))
            self.crash("after_resp_log_before_return")
            return resp
        return self._lstream(stream_id, body, blobs, reg, sent)

    def _lstream(self, stream_id, body, blobs, reg, sent):
        acc = shapes.AnthropicAcc() if self.provider == shapes.ANTHROPIC else shapes.OpenAIAcc()
        idx = 0
        for ev in self.t.stream(sent):
            self.crash("mid_stream", idx)
            idx += 1
            acc.feed(ev)
            term = (isinstance(ev, dict) and ev.get("type") == "message_stop") if self.provider == shapes.ANTHROPIC else ev == "[DONE]"
            if term:
                if self.provider == shapes.ANTHROPIC:
                    m, _ = acc.result()
                    msg, stop, rid = {"role": "assistant", "content": m["content"]}, m.get("stop_reason"), m.get("id")
                else:
                    r, _ = acc.result()
                    msg, stop, rid = r["choices"][0]["message"], acc.finish, r["id"]
                done = stop in shapes.COMPLETE_STOPS[self.provider]
                self._pending = (stream_id, body, blobs, reg, "complete" if done else "turn_incomplete", msg, stop, done, rid)
                self.crash("after_resp_log_before_return")
            yield ev

    def flush(self) -> None:
        if self._pending is None:
            return
        stream_id, body, blobs, reg, status, msg, stop, done, rid = self._pending
        self._pending = None
        _, n, _ = self._append(stream_id, "REQ", None, body, blobs, new_call=True)
        self._resp_record(stream_id, n, reg, status, msg, stop, done, [], rid, None, None)


class ArrivalKeyedIngester:
    """Control for item 2: same delta semantics (only messages beyond what the previous call covered), but the event key is the
    arrival counter, records apply on arrival (no hold for holes, no dedupe)."""

    def __init__(self, blob_source):
        self.blob = blob_source
        self.rows: List[dict] = []
        self.arrival = 0
        self.seen_len = 0
        self.provider = "anthropic"

    def deliver(self, records: List[dict]) -> None:
        for r in records:
            self.arrival += 1
            body = json.loads(r["body"])
            if r["kind"] == "REQ":
                self.provider = body["provider"]
                for m in body["msgs"][self.seen_len:]:
                    msg = json.loads(self.blob(m["h"]))
                    for b in shapes.blocks_of_message(self.provider, msg):
                        self.rows.append({"key": "arr%d" % self.arrival, "seq": r["seq"], "prov": b.prov, "content": cj(b.data)})
                self.seen_len = len(body["msgs"]) + 1
            elif r["kind"] == "RESP" and body.get("msg"):
                msg = json.loads(self.blob(body["msg"]["h"]))
                for b in shapes.blocks_of_message(self.provider, msg):
                    self.rows.append({"key": "arr%d" % self.arrival, "seq": r["seq"], "prov": b.prov, "content": cj(b.data)})


class RoleOnlyIngester:
    """Control for item 4: provenance = the role of the message as the request carries it; no injected registry. Ingests every block
    of every request message that is new by position (so a host-merged injected block lands in a user event)."""

    def __init__(self, blob_source, raw_requests: List[dict], provider: str = "anthropic"):
        self.blob, self.raw, self.provider = blob_source, raw_requests, provider

    def user_texts(self) -> List[str]:
        out = []
        seen = 0
        for req in self.raw:
            for m in req["messages"][seen:]:
                for b in shapes.blocks_of_message(self.provider, m):
                    if b.prov == "user" and isinstance(b.data, dict) and b.data.get("type") == "text":
                        out.append(b.data["text"])
            seen = len(req["messages"])
        return out


class TextRoleIngester:
    """Control for item 4 (second kind): decide 'injected' by a text marker regex over user text. Defeated by a variant marker."""
    MARK = re.compile(r"<memory[^>]*>|MEMCORE-[0-9a-f]{16}")

    def user_texts(self, texts: List[str]) -> List[str]:
        return [t for t in texts if not self.MARK.search(t)]


class ScanAfterWriteTap(PortTap):
    """Control for item 5: the record is written with the raw request first and scanned/rewritten afterwards."""

    def _red(self, obj, counter):                  # no redaction at write time (the scan happens afterwards, too late)
        return obj


class SharedOmissionIngester(Ingester):
    """Control for the independent oracle (PREREG-amend-3 A4): a systematic omission, here 'never write thinking events'. A baseline and
    an arm built from this same class agree with each other (delivery invariance holds) while both miss the same events; only an
    oracle built outside the ingest path sees it."""

    def _emit(self, stream, call_n, side, pos, blk, prov, kind, *a, **k):
        if kind == "thinking":
            return
        return super()._emit(stream, call_n, side, pos, blk, prov, kind, *a, **k)


class CorruptPayloadIngester(Ingester):
    """Control for the payload oracle (PREREG-amend-4 A2): keeps every tool id and empties tool arguments and tool-result content."""

    def _emit(self, stream, call_n, side, pos, blk, prov, kind, status, content, *a, **k):
        if kind in ("tool_call", "tool_result", "server_tool_call"):
            d = json.loads(content)
            if "input" in d:
                d["input"] = {}
            if "function" in d:
                d["function"] = dict(d["function"], arguments="{}")
            if "content" in d:
                d["content"] = ""
            if d.get("type") == "text":
                d["text"] = ""
            content = cj(d)
        return super()._emit(stream, call_n, side, pos, blk, prov, kind, status, content, *a, **k)
