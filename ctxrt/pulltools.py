"""P3: the pull tools (search, open, timeline, current_value) with a contract: bounded output, opaque ids, snapshot-pinned pagination, typed
"nothing found", and current-value answers taken from the decision state (never from search hits). Built on memcore.ports.AgentToolSurface so
the schema check and the egress guard sit on every call."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from typing import Dict, List, Optional

from memcore.ports import AgentToolSurface, ToolSpec
from ctxrt.decisions import DecisionLog, req_id
from ctxrt.kernel import MemoryLog

MAX_CHARS = 6000
SNIPPET = 240
CHUNK = 3000
NO_RESULT_REASONS = ("no_match", "as_of_before_history", "empty_store", "cursor_exhausted", "unknown_subject", "unknown_predicate")


def toks(s: str):
    return set(re.findall(r"\w+", s.lower()))


def tf_score(qt, text: str) -> int:
    """Term-frequency overlap (each query term counts up to 3 times): repeated terms rank higher, as in any lexical scorer."""
    words = re.findall(r"\w+", text.lower())
    return sum(min(3, words.count(t)) for t in qt)


class PullTools:
    def __init__(self, log: MemoryLog, decisions: DecisionLog, secret: bytes = b"ctxrt-cursor", max_chars: int = MAX_CHARS):
        self.log, self.dec, self._secret, self.max_chars = log, decisions, secret, max_chars
        self._reg: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- opaque tokens
    def _tok(self, prefix: str, state: dict) -> str:
        raw = json.dumps(state, sort_keys=True)
        t = prefix + hmac.new(self._secret, raw.encode(), hashlib.sha256).hexdigest()[:24]
        with self._lock:
            self._reg[t] = state
        return t

    def _untok(self, t: str) -> Optional[dict]:
        with self._lock:
            return self._reg.get(t)

    # ---- bounded rendering
    def _fit(self, res: dict, key: str) -> dict:
        def size():       # 160 reserved for the keys added after fitting (truncated, omitted, next_cursor)
            return len(json.dumps(res, ensure_ascii=False)) + 160
        omitted = 0
        while size() > self.max_chars and res.get(key):
            res[key].pop()
            omitted += 1
        if omitted:
            res["truncated"] = True
            res["omitted"] = omitted
        res.setdefault("truncated", False)
        return res

    # ---- search / timeline share a snapshot-pinned keyset pagination
    def _page(self, kind: str, query: str, as_of: Optional[str], limit: int, cursor: Optional[str]) -> dict:
        if cursor:
            st = self._untok(cursor)
            if st is None or st["kind"] != kind:
                return {"status": "error", "code": "invalid_cursor"}
            snap, after, q, as_of = st["snap"], st["after"], st["q"], st["as_of"]
        else:
            snap, after, q = self.log.watermark(), None, query
        evs = self.log.upto(snap)
        if not evs:
            return {"status": "no_results", "reason": "empty_store"}
        if as_of is not None and as_of < min(e["ts"] for e in evs):
            return {"status": "no_results", "reason": "as_of_before_history"}
        qt = toks(q)
        cand = []
        for e in evs:
            if as_of is not None and e["ts"] > as_of:
                continue
            if kind == "search":
                sc = tf_score(qt, e["text"])
                if sc:
                    cand.append(((-sc, e["seq"]), e, sc))
            else:
                if q.lower() in [x.lower() for x in e["entities"]] or q.lower() in e["text"].lower():
                    cand.append(((e["ts"], e["seq"]), e, 0))
        cand.sort(key=lambda t: t[0])
        if after is not None:
            cand = [c for c in cand if list(c[0]) > after]
        if not cand:
            return {"status": "no_results", "reason": "cursor_exhausted" if cursor else "no_match"}
        page = cand[:limit]
        results = [{"id": e["oid"], "ts": e["ts"], "speaker": e["speaker"], "snippet": e["text"][:SNIPPET], **({"score": sc} if kind == "search" else {})} for _, e, sc in page]
        out = {"status": "ok", "results": results, "checked_through": self._tok("w_", {"wm": snap})}
        out = self._fit(out, "results")
        shown = len(out["results"])
        if shown == 0:
            return {"status": "error", "code": "result_too_large"}
        more = len(cand) > shown
        last = page[shown - 1][0]
        out["next_cursor"] = self._tok("c_", {"kind": kind, "snap": snap, "after": list(last), "q": q, "as_of": as_of}) if more else None
        return out

    def search(self, query: str, as_of: Optional[str] = None, limit: int = 5, cursor: Optional[str] = None) -> dict:
        return self._page("search", query, as_of, limit, cursor)

    def timeline(self, entity: str, limit: int = 5, cursor: Optional[str] = None) -> dict:
        return self._page("timeline", entity, None, limit, cursor)

    def open(self, id: str, offset: int = 0) -> dict:
        e = self.log.by_oid(id)
        if e is None:
            return {"status": "not_found"}
        text = e["text"]
        chunk = text[offset: offset + CHUNK]
        res = {"status": "ok", "id": e["oid"], "ts": e["ts"], "speaker": e["speaker"], "offset": offset, "text": chunk,
               "next_offset": (offset + CHUNK) if offset + CHUNK < len(text) else None, "total_chars": len(text), "truncated": offset + CHUNK < len(text)}
        while len(json.dumps(res, ensure_ascii=False)) > self.max_chars and len(res["text"]) > 1:
            res["text"] = res["text"][: len(res["text"]) // 2]
            res["next_offset"] = offset + len(res["text"])
            res["truncated"] = True
        return res

    def current_value(self, subject: str, predicate: str, as_of: Optional[str] = None) -> dict:
        r = req_id(subject, predicate)
        first = self.dec.first_ts(subject, predicate)
        pos = self.dec.position()
        cthrough = self._tok("w_", {"dpos": pos})
        if first is None:
            return {"status": "no_results", "reason": "unknown_subject", "checked_through": cthrough}
        if as_of is not None and as_of < first:
            return {"status": "no_results", "reason": "as_of_before_history", "checked_through": cthrough}
        st = self.dec.fold(as_of=as_of, upto=pos)
        q = st["reqs"][r]
        dec_oid = lambda d: "dc_" + hmac.new(self._secret, ("dec-" + str(d)).encode(), hashlib.sha256).hexdigest()[:20]
        src = lambda d: (self.log.oid(self.dec.source_of(d)) if d and self.dec.source_of(d) else None)
        base = {"checked_through": cthrough, "decision_event": dec_oid(q["value_event"]) if q["value_event"] else None,
                "evidence": src(q["value_event"]), "superseded": [{"value": c["value"], "decision_event": dec_oid(c["event"])} for c in q["chain"][-5:]]}
        if q["status"] == "current":
            return dict(base, status="ok", value=q["value"])
        if q["status"] == "retracted":
            return dict(base, status="retracted")
        if q["status"] == "contested":
            return dict(base, status="contested", candidates=[dec_oid(c["event"]) for c in q["contested"]["candidates"]])
        return {"status": "no_results", "reason": "unknown_subject", "checked_through": cthrough}

    # ---- the agent surface (schema + egress guard on every call)
    def surface(self, egress_guard="default") -> AgentToolSurface:
        S = {"type": "string", "maxLength": 500}
        L = {"type": "integer", "minimum": 1, "maximum": 10}

        def obj(p, req):
            return {"type": "object", "properties": p, "required": req, "additionalProperties": False}
        return AgentToolSurface([
            ToolSpec("search", "Search remembered events. Returns ids, snippets, a cursor.", obj({"query": S, "as_of": S, "limit": L, "cursor": S}, ["query"]),
                     lambda a: self.search(a["query"], a.get("as_of"), a.get("limit", 5), a.get("cursor"))),
            ToolSpec("open", "Open one event by id (chunked).", obj({"id": S, "offset": {"type": "integer", "minimum": 0}}, ["id"]),
                     lambda a: self.open(a["id"], a.get("offset", 0))),
            ToolSpec("timeline", "Chronological events mentioning an entity.", obj({"entity": S, "limit": L, "cursor": S}, ["entity"]),
                     lambda a: self.timeline(a["entity"], a.get("limit", 5), a.get("cursor"))),
            ToolSpec("current_value", "The current value of a subject's predicate, from the decision state.", obj({"subject": S, "predicate": S, "as_of": S}, ["subject", "predicate"]),
                     lambda a: self.current_value(a["subject"], a["predicate"], a.get("as_of"))),
        ], egress_guard=egress_guard)
