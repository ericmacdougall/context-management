"""Shared builders for tests and proof runs."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

from memcore.memtools import BudgetedInjector, InMemoryStore

from . import shapes
from .ingester import Ingester
from .portlog import PortLog
from .scan import ReferenceScanner
from .canon import Hasher
from .stubs import Stub, Truth, text, tool_use, tool_result, thinking
from .tap import PortTap, StaticIdentity

KEY = b"tenant-key-for-tests-only"


def make_injector(texts=("Dana prefers tea over coffee", "Dana's dog is called Biscuit")) -> BudgetedInjector:
    st = InMemoryStore()
    for i, t in enumerate(texts):
        st.add_event("ev%d" % i, "s1", "2026-01-01T00:00:00Z", t)
    return BudgetedInjector(st, char_budget=800, k=5, egress_guard=None)


class Rig:
    def __init__(self, tmp: str, provider: str = "anthropic", script: Optional[List[dict]] = None, session: str = "sess1", tenant: str = "t1",
                 injector=None, stream: bool = False, tap_kw: Optional[dict] = None, truth: bool = False, stub_kw: Optional[dict] = None,
                 sync: str = "FULL"):
        self.tmp, self.provider, self.stream = tmp, provider, stream
        self.logp = os.path.join(tmp, "port.db")
        self.log = PortLog(self.logp, sync=sync)
        self.truth = Truth(os.path.join(tmp, "truth.jsonl") if truth else None)
        self.stub = Stub(provider, script or [], self.truth, **(stub_kw or {}))
        self.hasher = Hasher(KEY)
        self.identity = StaticIdentity(tenant, session)
        self.stream_id = "%s/%s" % (tenant, session)
        self.tap = PortTap(self.stub, provider, self.log, self.identity, self.hasher, ReferenceScanner(), injector=injector, **(tap_kw or {}))
        self.ing = Ingester(os.path.join(tmp, "events.db"), self.log.blob, sync=sync)
        self.msgs: List[dict] = []
        self.call_kw: dict = {}              # keywords passed to every tap.call (e.g. {'new_count': 1}: a host that declares its boundary)

    def ask(self, user: Any, **kw):
        """Append a user message, call through the tap, append the assistant response (as a well-behaved host would)."""
        self.msgs.append({"role": "user", "content": user})
        req = self.request()
        out = self.call(req)
        if self.provider == "anthropic":
            self.msgs.append({"role": "assistant", "content": out["content"]})
        else:
            self.msgs.append(out["choices"][0]["message"])
        return out

    def request(self) -> dict:
        return {"model": "stub-model", "max_tokens": 256, "messages": [dict(m) for m in self.msgs]}

    def call(self, req: dict, **kw):
        kw = {**self.call_kw, **kw}
        if not self.stream:
            return self.tap.call(req, **kw)
        acc = shapes.AnthropicAcc() if self.provider == "anthropic" else shapes.OpenAIAcc()
        for ev in self.tap.call(req, stream=True, **kw):
            acc.feed(ev)
        m, _ = acc.result()
        return m if self.provider == "anthropic" else m

    def sync(self) -> int:
        return self.ing.sync(self.log, self.stream_id)

    def events(self, **kw):
        return self.ing.events(self.stream_id, **kw)
