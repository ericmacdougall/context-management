"""Egress guard at the port: keep real dataset session ids and label-bearing id patterns out of every model-facing string
(prompts, system prompts, tool results, injected blocks). One implementation for this layer; the pattern follows the eval guard
(gold sessions `answer_<hex>_<n>`, fillers `sharegpt_`, `ultrachat_`) plus an optional set of exact real ids matched as whole tokens."""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Set

from .ports import PortError

PATTERN = re.compile(r"(?:^|[^A-Za-z0-9])(answer_[0-9a-f]{4,}|sharegpt_[A-Za-z0-9]+|ultrachat_\d+)")
TOKEN = re.compile(r"[A-Za-z0-9_]+")


class EgressLeak(PortError, ValueError):
    pass


class EgressGuard:
    def __init__(self, real_ids: Optional[Iterable[str]] = None, extra_patterns: Iterable[str] = ()):
        self.real_ids: Set[str] = set(real_ids or ())
        self.extra = [re.compile(p) for p in extra_patterns]

    def check_text(self, s: str, where: str = "text") -> None:
        if PATTERN.search(s) or any(p.search(s) for p in self.extra):
            raise EgressLeak("label-bearing session id pattern in %s" % where)
        if self.real_ids and any(t in self.real_ids for t in TOKEN.findall(s)):
            raise EgressLeak("real session id in %s" % where)

    def check(self, obj: Any, where: str = "value") -> None:
        if isinstance(obj, str):
            self.check_text(obj, where)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                self.check_text(str(k), where + "/key")
                self.check(v, "%s/%s" % (where, k))
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                self.check(v, "%s[%d]" % (where, i))


DEFAULT_GUARD = EgressGuard()
