"""P3: the pull tools (search, open, timeline, current_value) with a contract: bounded output (UTF-8 bytes of the serialised result), opaque ids,
tenant and principal scope resolved SERVER-SIDE from the authenticated session (never from arguments), signed self-contained cursors bound to their
owner, snapshot-pinned pagination, typed "nothing found", and current-value answers taken from the decision state (never from search hits).
Paging contract: a cursor pins a log watermark and every score is computed from events at or before it, so a scorer that uses corpus statistics
(idf) is stable too. A scorer that reads LIVE statistics is NOT covered (see the control in proofs/p3_pull.py).
Built on memcore.ports.AgentToolSurface so the schema check and the egress guard sit on every call."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional

from memcore.ports import AgentToolSurface, ToolSpec
from ctxrt.decisions import DecisionLog, req_id
from ctxrt.kernel import MemoryLog

MAX_BYTES = 8000
SNIPPET = 240
CHUNK_CHARS = 2500
MAX_TOKEN = 2048
NO_RESULT_REASONS = ("no_match", "as_of_before_history", "empty_store", "cursor_exhausted", "unknown_subject", "unknown_predicate")


@dataclass(frozen=True)
class CallerContext:
    """Resolved by the host from the authenticated session. Tools never read these from their arguments."""
    tenant: str
    principal: str
    sessions: FrozenSet[str]


def toks(s: str):
    return set(re.findall(r"\w+", s.lower()))


def tf_score(qt, text: str) -> int:
    words = re.findall(r"\w+", text.lower())
    return sum(min(3, words.count(t)) for t in qt)


def nbytes(o) -> int:
    return len(json.dumps(o, ensure_ascii=False).encode("utf-8"))


class PullTools:
    def __init__(self, log: MemoryLog, decisions: DecisionLog, secret: bytes, max_bytes: int = MAX_BYTES, scorer: str = "tf"):
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise ValueError("PullTools needs a configured secret of at least 16 bytes")
        self.log, self.dec, self._secret, self.max_bytes, self.scorer = log, decisions, bytes(secret), max_bytes, scorer

    # ---- signed self-contained tokens (no registry: bounded, survive restarts, valid behind a load balancer)
    def _sign(self, state: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        tok = body + "." + mac
        if len(tok) > MAX_TOKEN:
            raise ValueError("token too large")
        return tok

    def _unsign(self, tok: str, ctx: CallerContext) -> Optional[dict]:
        if not isinstance(tok, str) or len(tok) > MAX_TOKEN or "." not in tok:
            return None
        body, mac = tok.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]):
            return None
        try:
            st = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except ValueError:
            return None
        if st.get("o") != ctx.principal or st.get("t") != ctx.tenant:        # bound to its owner
            return None
        return st

    def _visible(self, ctx: CallerContext, e: dict) -> bool:
        return e["tenant"] == ctx.tenant and e["session"] in ctx.sessions

    # ---- bounded rendering (bytes of the serialised JSON)
    def _fit(self, res: dict, key: str) -> dict:
        omitted = 0
        while nbytes(res) + 200 > self.max_bytes and res.get(key):    # 200 bytes reserved for keys added after fitting
            res[key].pop()
            omitted += 1
        if omitted:
            res["truncated"] = True
            res["omitted"] = omitted
        res.setdefault("truncated", False)
        return res

    def _scores(self, qt, evs):
        if self.scorer == "tf":
            return {e["seq"]: tf_score(qt, e["text"]) for e in evs}
        n = len(evs)
        df = {t: 0 for t in qt}
        words = {}
        for e in evs:
            w = re.findall(r"\w+", e["text"].lower())
            words[e["seq"]] = w
            for t in qt:
                df[t] += t in set(w)
        return {e["seq"]: round(sum(min(3, words[e["seq"]].count(t)) * math.log(1 + n / (1 + df[t])) for t in qt), 6) for e in evs}

    def _page(self, ctx: CallerContext, kind: str, query: str, as_of: Optional[str], limit: int, cursor: Optional[str]) -> dict:
        if cursor:
            st = self._unsign(cursor, ctx)
            if st is None or st.get("k") != kind:
                return {"status": "error", "code": "invalid_cursor"}
            snap, after, q, as_of = st["snap"], st["after"], st["q"], st["as_of"]
        else:
            snap, after, q = self.log.watermark(), None, query
        evs = [e for e in self.log.upto(snap) if self._visible(ctx, e)]
        if not evs:
            return {"status": "no_results", "reason": "empty_store"}
        if as_of is not None and as_of < min(e["ts"] for e in evs):
            return {"status": "no_results", "reason": "as_of_before_history"}
        qt = toks(q)
        scores = self._scores(qt, evs) if kind == "search" else {}
        cand = []
        for e in evs:
            if as_of is not None and e["ts"] > as_of:
                continue
            if kind == "search":
                sc = scores[e["seq"]]
                if sc:
                    cand.append(((-sc, e["seq"]), e, sc))
            elif q.lower() in [x.lower() for x in e["entities"]] or q.lower() in e["text"].lower():
                cand.append(((e["ts"], e["seq"]), e, 0))
        cand.sort(key=lambda t: t[0])
        if after is not None:
            cand = [c for c in cand if list(c[0]) > after]
        if not cand:
            return {"status": "no_results", "reason": "cursor_exhausted" if cursor else "no_match"}
        page = cand[:limit]
        results = [{"id": e["oid"], "ts": e["ts"], "speaker": e["speaker"], "snippet": e["text"][:SNIPPET], **({"score": sc} if kind == "search" else {})} for _, e, sc in page]
        ct = self._sign({"o": ctx.principal, "t": ctx.tenant, "wm": snap})
        shown = len(results)
        while True:
            out = {"status": "ok", "results": results[:shown], "checked_through": ct, "truncated": shown < len(results)}
            if shown < len(results):
                out["omitted"] = len(results) - shown
            more = len(cand) > shown
            out["next_cursor"] = self._sign({"o": ctx.principal, "t": ctx.tenant, "k": kind, "snap": snap, "after": list(page[shown - 1][0]), "q": q, "as_of": as_of}) if (more and shown) else None
            if nbytes(out) <= self.max_bytes or shown == 0:
                break
            shown -= 1
        if shown == 0:
            return {"status": "error", "code": "result_too_large"}
        return out

    def search(self, ctx, query, as_of=None, limit=5, cursor=None):
        return self._page(ctx, "search", query, as_of, limit, cursor)

    def timeline(self, ctx, entity, limit=5, cursor=None):
        return self._page(ctx, "timeline", entity, None, limit, cursor)

    def open(self, ctx: CallerContext, id: str, offset: int = 0) -> dict:
        e = self.log.by_oid(id)
        if e is None or not self._visible(ctx, e):            # same answer for "unknown" and "not yours": no existence oracle
            return {"status": "not_found"}
        text = e["text"]
        n = CHUNK_CHARS
        while True:
            chunk = text[offset: offset + n]
            res = {"status": "ok", "id": e["oid"], "ts": e["ts"], "speaker": e["speaker"], "offset": offset, "text": chunk,
                   "next_offset": (offset + len(chunk)) if offset + len(chunk) < len(text) else None, "total_chars": len(text),
                   "truncated": offset + len(chunk) < len(text)}
            if nbytes(res) <= self.max_bytes or n <= 1:
                return res
            n = max(1, n // 2)

    def current_value(self, ctx: CallerContext, subject: str, predicate: str, as_of: Optional[str] = None) -> dict:
        r = req_id(subject, predicate, ctx.tenant)
        first = self.dec.first_ts(subject, predicate, ctx.tenant)
        pos = self.dec.position()
        cthrough = self._sign({"o": ctx.principal, "t": ctx.tenant, "dpos": pos})
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

    # ---- the agent surface: the caller context comes from the host, never from the model's arguments
    def surface(self, ctx_provider: Callable[[], CallerContext], egress_guard="default") -> AgentToolSurface:
        if egress_guard is None:
            raise ValueError("a PASS arm needs the egress guard on (Amendment 1)")
        S = {"type": "string", "maxLength": 500}
        L = {"type": "integer", "minimum": 1, "maximum": 10}

        def obj(p, req):
            return {"type": "object", "properties": p, "required": req, "additionalProperties": False}
        return AgentToolSurface([
            ToolSpec("search", "Search remembered events. Returns ids, snippets, a cursor.", obj({"query": S, "as_of": S, "limit": L, "cursor": {"type": "string", "maxLength": MAX_TOKEN}}, ["query"]),
                     lambda a: self.search(ctx_provider(), a["query"], a.get("as_of"), a.get("limit", 5), a.get("cursor"))),
            ToolSpec("open", "Open one event by id (chunked).", obj({"id": S, "offset": {"type": "integer", "minimum": 0}}, ["id"]),
                     lambda a: self.open(ctx_provider(), a["id"], a.get("offset", 0))),
            ToolSpec("timeline", "Chronological events mentioning an entity.", obj({"entity": S, "limit": L, "cursor": {"type": "string", "maxLength": MAX_TOKEN}}, ["entity"]),
                     lambda a: self.timeline(ctx_provider(), a["entity"], a.get("limit", 5), a.get("cursor"))),
            ToolSpec("current_value", "The current value of a subject's predicate, from the decision state.", obj({"subject": S, "predicate": S, "as_of": S}, ["subject", "predicate"]),
                     lambda a: self.current_value(ctx_provider(), a["subject"], a["predicate"], a.get("as_of"))),
        ], egress_guard=egress_guard)
