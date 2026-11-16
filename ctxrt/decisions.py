"""Decision state for current-value answers: the vendored pol/core.py reducer (515e594) over supersede / retract events. One (subject,
predicate) pair is one requirement id. The state is a pure function of the decision log, cut at a time (as_of) or at a log position."""
from __future__ import annotations

import copy
import threading
from typing import Dict, List, Optional

from ctxrt.vendor.pol import core as C

PRINCIPALS = {"owner": "human", "owner2": "human"}


def req_id(subject: str, predicate: str, tenant: str = "t0") -> str:
    return "%s|%s|%s" % (tenant, subject.strip().lower(), predicate.strip().lower())


def _ensure(state: dict, req: str) -> dict:
    if req in state["reqs"]:
        return state
    ns = dict(state)
    ns["authority"] = dict(state["authority"])
    ns["authority"][req] = sorted(PRINCIPALS)
    ns["reqs"] = dict(state["reqs"])
    ns["reqs"][req] = C.new_req()
    return ns


def genesis() -> dict:
    return C.genesis(PRINCIPALS, {}, ["owner"])


def replay(events: List[dict]) -> dict:
    st = genesis()
    for e in events:
        st = _ensure(st, e["req"])
        st, _ = C.apply(st, e["ev"])
    return st


class DecisionLog:
    """Append-only list of reducer events with a ts and the id of the memory event that is the evidence. Thread-safe."""

    def __init__(self):
        self._lock = threading.RLock()
        self.items: List[dict] = []
        self._live = genesis()
        self._n = 0

    def _append(self, kind: str, subject: str, predicate: str, ts: str, source_key: Optional[str], principal: str, value=None, based_on: Optional[int] = None, tenant: str = "t0") -> str:
        with self._lock:
            if self.items and ts < self.items[-1]["ts"]:
                raise ValueError("decision log is time-ordered")
            req = req_id(subject, predicate, tenant)
            self._live = _ensure(self._live, req)
            self._n += 1
            did = "d%d" % self._n
            b = self._live["reqs"][req]["version"] if based_on is None else based_on
            ev = {"type": kind, "id": did, "idem": did, "req": req, "principal": principal, "channel": "owner_ui", "based_on": b}
            if kind == "supersede":
                ev["value"] = value
            self._live, code = C.apply(self._live, ev)
            self.items.append({"req": req, "ev": ev, "ts": ts, "source_key": source_key, "code": code, "pos": len(self.items) + 1})
            return did

    def set(self, subject, predicate, value, ts, source_key=None, principal="owner", based_on=None, tenant="t0") -> str:
        return self._append("supersede", subject, predicate, ts, source_key, principal, value, based_on, tenant)

    def retract(self, subject, predicate, ts, source_key=None, principal="owner", based_on=None, tenant="t0") -> str:
        return self._append("retract", subject, predicate, ts, source_key, principal, None, based_on, tenant)

    def position(self) -> int:
        with self._lock:
            return len(self.items)

    def fold(self, as_of: Optional[str] = None, upto: Optional[int] = None) -> dict:
        with self._lock:
            its = [i for i in self.items[: upto if upto is not None else len(self.items)] if as_of is None or i["ts"] <= as_of]
        return replay(its)

    def first_ts(self, subject: str, predicate: str, tenant: str = "t0") -> Optional[str]:
        r = req_id(subject, predicate, tenant)
        with self._lock:
            for i in self.items:
                if i["req"] == r:
                    return i["ts"]
        return None

    def source_of(self, did: str) -> Optional[str]:
        with self._lock:
            for i in self.items:
                if i["ev"]["id"] == did:
                    return i["source_key"]
        return None
