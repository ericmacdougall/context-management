"""Shared kernel: append-only memory log with origin and speaker, hash-chain digest, sealed watermark (STUB seal: the write-path seal belongs to
another stream), and checkpoint/lineage records. Thread-safe. Internal keys and session ids never leave this module in model-facing output."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MEMORY_ORIGINS = ("user", "assistant", "tool")      # origins a memory event may carry; analyst/injected/summary text is never an event origin


def sha(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()


class MemoryLog:
    def __init__(self, secret: bytes = b"ctxrt-test-secret"):
        self._lock = threading.RLock()
        self.events: List[dict] = []
        self._secret = secret
        self._by_oid: Dict[str, int] = {}

    # ids
    def oid(self, key: str) -> str:
        return "m_" + hmac.new(self._secret, key.encode(), hashlib.sha256).hexdigest()[:20]

    def append(self, session: str, ts: str, origin: str, text: str, speaker: Optional[str] = None, round: Optional[int] = None,
               entities: Optional[List[str]] = None, kind: str = "statement", meta: Optional[dict] = None) -> int:
        if origin not in MEMORY_ORIGINS:
            raise ValueError("origin %r may not become a memory event" % origin)
        with self._lock:
            seq = len(self.events) + 1
            key = "ev-%s-%d" % (session, seq)
            prev = self.events[-1]["chain"] if self.events else "0"
            e = {"seq": seq, "key": key, "oid": self.oid(key), "session": session, "ts": ts, "origin": origin, "speaker": speaker or origin, "round": round,
                 "text": text, "entities": list(entities or []), "kind": kind, "meta": dict(meta or {})}
            e["chain"] = sha([prev, e["key"], e["ts"], e["origin"], e["text"]])
            self.events.append(e)
            self._by_oid[e["oid"]] = seq
            return seq

    def watermark(self) -> int:
        with self._lock:
            return len(self.events)

    def upto(self, wm: int) -> List[dict]:
        with self._lock:
            return [dict(e) for e in self.events[:wm]]

    def by_oid(self, oid: str) -> Optional[dict]:
        with self._lock:
            s = self._by_oid.get(oid)
            return dict(self.events[s - 1]) if s else None

    def digest(self, wm: int) -> str:
        with self._lock:
            return self.events[wm - 1]["chain"] if wm else "0"


@dataclass(frozen=True)
class Seal:
    """STUB checkpoint seal: watermark plus the chain digest of the log prefix. (The real seal is the write-path stream's.)"""
    watermark: int
    digest: str

    @staticmethod
    def of(log: MemoryLog, wm: Optional[int] = None) -> "Seal":
        wm = log.watermark() if wm is None else wm
        return Seal(wm, log.digest(wm))

    def verify(self, log: MemoryLog) -> bool:
        return log.watermark() >= self.watermark and log.digest(self.watermark) == self.digest
