"""Opaque ids for everything a model sees (analyst, reader, extractor). Real LongMemEval session ids leak the label:
gold sessions are named `answer_<hash>_<n>` and filler sessions `sharegpt_`, `ultrachat_` or hex ids, so a model
shown real ids could pick gold by name. Opaque id = "s" + first 12 hex of sha256("16096:anon:<session id>");
round ids keep the round index: "<opaque session>:<index>". Deterministic; reversible only through a lookup over the
known session ids (never written into a file a model reads).
"""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable


def opaque_sid(sid: str) -> str:
    return "s" + hashlib.sha256(("16096:anon:" + sid).encode()).hexdigest()[:12]


def opaque_rid(rid: str) -> str:
    sid, idx = rid.rsplit(":", 1)
    return "%s:%s" % (opaque_sid(sid), idx)


class AnonMap:
    def __init__(self, session_ids: Iterable[str]):
        self.rev: Dict[str, str] = {opaque_sid(s): s for s in session_ids}
        if len(self.rev) != len(set(session_ids)):
            raise ValueError("opaque id collision")

    def real_rid(self, orid: str) -> str:
        sid, idx = orid.rsplit(":", 1)
        return "%s:%s" % (self.rev[sid], idx)

    def real_sid(self, osid: str) -> str:
        return self.rev[osid]
