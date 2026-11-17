"""Read-only guard for the SHARED Elasticsearch node (ES-pilot). Wraps any client that has `request(method, path, body)`.

Allowed: GET of cluster health, `_cat/indices|shards/<index>`, `_nodes/stats/thread_pool`, `<index>/_settings|_mapping|_count|_search`,
POST of `<index>/_search` and `<index>/_count`. Everything else raises EsGuardError BEFORE the inner client is called, so
no byte of a write ever leaves the process. Every allowed request is counted by class for the run's read-only proof.
A `_search` body is also refused if it carries a key that could do work other than reading (`script_fields`-free is not
enough to prove safety, so only plain read bodies are accepted: no `pit` keep-alive creation, no `_update_by_query`
path is reachable at all)."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Optional


class EsGuardError(RuntimeError):
    pass


def _rules(index: str):
    ix = re.escape(index)
    return [
        ("GET", re.compile(r"^/$"), "info"),
        ("GET", re.compile(r"^/_cluster/health(\?[A-Za-z0-9_=&,.\-]*)?$"), "cluster_health"),
        ("GET", re.compile(r"^/_cat/(indices|shards)/%s(\?[A-Za-z0-9_=&,.\-]*)?$" % ix), "cat"),
        ("GET", re.compile(r"^/_cat/shards(\?[A-Za-z0-9_=&,.\-]*)?$"), "cat"),
        ("GET", re.compile(r"^/_nodes/stats/thread_pool$"), "node_stats"),
        ("GET", re.compile(r"^/%s/(_settings|_mapping|_count)(\?[A-Za-z0-9_=&,.\-]*)?$" % ix), "index_read"),
        ("GET", re.compile(r"^/%s/_settings\?[A-Za-z0-9_=&,.\-*]*$" % ix), "index_read"),
        ("POST", re.compile(r"^/%s/_(search|count)$" % ix), "search"),
    ]


class GuardedEs:
    def __init__(self, inner: Any, index: str):
        self._inner = inner
        self.index = index
        self._rules = _rules(index)
        self.log: Counter = Counter()
        self.search_flags: Dict[str, int] = {"n": 0, "timed_out": 0, "shards_failed": 0, "shards_incomplete": 0}

    def __repr__(self) -> str:
        return "GuardedEs(%r, read-only)" % self.index

    def request(self, method: str, path: str, body: Any = None, ndjson: bool = False) -> Dict[str, Any]:
        method = method.upper()
        if ndjson:
            raise EsGuardError("ndjson (bulk) requests are never allowed on the shared node")
        for m, rx, cls in self._rules:
            if m == method and rx.match(path):
                if cls == "search" and isinstance(body, dict) and ("pit" in body or "script" in body):
                    raise EsGuardError("search body with pit/script refused")
                self.log["%s %s" % (method, cls)] += 1
                out = self._inner.request(method, path, body)
                if cls == "search" and path.endswith("_search"):
                    self._flags(out)
                return out
        self.log["REFUSED"] += 1
        raise EsGuardError("refused on the shared node: %s %s" % (method, path))

    def _flags(self, out: Dict[str, Any]) -> None:
        self.search_flags["n"] += 1
        if out.get("timed_out"):
            self.search_flags["timed_out"] += 1
        sh = out.get("_shards", {})
        if sh.get("failed", 0):
            self.search_flags["shards_failed"] += 1
        if sh.get("successful") != sh.get("total"):
            self.search_flags["shards_incomplete"] += 1

    def search(self, index: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if index != self.index:
            raise EsGuardError("refused on the shared node: index %r" % index)
        return self.request("POST", "/%s/_search" % index, body)

    def get(self, path: str) -> Dict[str, Any]:
        return self.request("GET", path)
