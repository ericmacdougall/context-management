"""SearchBackend over Elasticsearch 9.5 (stdlib urllib; request shapes mirror elasticsearch-py's _search).

One index per tenant BY CONSTRUCTION: the adapter is bound to a tenant, derives the physical index name
`<prefix><tenant>-<logical>` itself, rejects wildcards, commas and path tricks, and raises CrossTenantError for any call that names
another tenant. Fused search has two strategies behind one call: "backend" (retriever rrf or linear; needs Enterprise or trial
licence) and "client" (memcore.fusion.rrf over a lexical and a vector search). strategy="auto" probes once: a licence error
(HTTP 403 / non-compliant) falls back to client; SearchResult.fusion reports which ran.
Credentials: the caller supplies auth_header() (never stored, logged or cached here).
"""
from __future__ import annotations

import json
import re
import socket
import time
import ssl
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence

from ..fusion import rrf
from ..http_util import urlopen as _urlopen
from ..ports import BackendError, CallTimeout, Hit, PortError, SearchBackend, SearchResult

# '-' separates tenant from logical name in the physical index, so a TENANT may not contain it (no dots either): the split is
# unambiguous. Logical names may contain '-' but not '.'; fullmatch (never $) so a trailing newline cannot slip through.
_TENANT = re.compile(r"[a-z0-9][a-z0-9_]{0,62}")
_LOGICAL = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_PREFIX = re.compile(r"[a-z0-9][a-z0-9_]{0,30}-")
TENANT_FIELD = "tenant"


class CrossTenantError(PortError):
    pass


class ElasticsearchSearch(SearchBackend):
    def __init__(self, base_url: str, auth_header: Callable[[], str], tenant: str, prefix: str = "mc-", text_field: str = "text",
                 vector_field: str = "vector", strategy: str = "auto", ca_file: Optional[str] = None, verify_tls: bool = True,
                 timeout_s: float = 30.0, num_candidates: int = 100, rescore_oversample: Optional[float] = None,
                 rank_window: int = 100, backend_fusion: str = "rrf"):
        if not _TENANT.fullmatch(tenant or ""):
            raise ValueError("bad tenant %r (letters, digits, underscore only: '-' is the separator)" % (tenant,))
        if not _PREFIX.fullmatch(prefix or ""):
            raise ValueError("bad prefix %r (must end with '-')" % (prefix,))
        if strategy not in ("auto", "backend", "client"):
            raise ValueError("strategy must be auto|backend|client")
        self.base, self._auth, self.tenant, self.prefix = base_url.rstrip("/"), auth_header, tenant, prefix
        self.text_field, self.vector_field, self.strategy, self.timeout_s = text_field, vector_field, strategy, timeout_s
        self.num_candidates, self.rescore_oversample, self.rank_window, self.backend_fusion = num_candidates, rescore_oversample, rank_window, backend_fusion
        self._ctx = None
        if base_url.startswith("https"):
            self._ctx = ssl.create_default_context(cafile=ca_file)
            if not verify_tls:
                self._ctx.check_hostname, self._ctx.verify_mode = False, ssl.CERT_NONE
        self._probe: Optional[bool] = None   # True = backend fusion usable; False = licence error seen; None = unknown
        self._probe_at = 0.0
        self.probe_ttl_s = 300.0
        self.fusion_location = "unknown"
        self.probe_error: Optional[str] = None

    # ---- index naming / tenancy
    def index_name(self, logical: str, tenant: Optional[str] = None) -> str:
        if tenant is not None and tenant != self.tenant:
            raise CrossTenantError("adapter is bound to tenant %r, call named %r" % (self.tenant, tenant))
        if not _LOGICAL.fullmatch(logical or ""):
            raise ValueError("bad logical index name %r" % (logical,))
        return "%s%s-%s" % (self.prefix, self.tenant, logical)

    # ---- transport
    def _req(self, method: str, path: str, body: Optional[dict] = None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": self._auth()})
        try:
            with _urlopen(req, timeout=self.timeout_s, context=self._ctx) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise BackendError("HTTP %d: %s" % (e.code, e.read()[:400].decode("utf-8", "replace"))) from None
        except (socket.timeout, TimeoutError):
            raise CallTimeout("elasticsearch did not answer in %.0fs" % self.timeout_s)
        except urllib.error.URLError as e:
            raise BackendError("connection failed: %s" % e.reason)

    def _filters(self, filter: Optional[dict]) -> List[dict]:
        if filter and TENANT_FIELD in filter and filter[TENANT_FIELD] != self.tenant:
            raise CrossTenantError("caller filter names tenant %r" % (filter[TENANT_FIELD],))
        # defence in depth: every query also filters on the tenant keyword field, whatever the index name says
        return [{"term": {TENANT_FIELD: self.tenant}}] + [{"term": {k: v}} for k, v in (filter or {}).items() if k != TENANT_FIELD]

    # ---- request builders (pure; unit-tested without a node)
    def lexical_body(self, query: str, filter, k: int) -> dict:
        return {"size": k, "query": {"bool": {"must": [{"match": {self.text_field: query}}], "filter": self._filters(filter)}}}

    def knn_clause(self, vector, filter, k: int) -> dict:
        knn = {"field": self.vector_field, "query_vector": list(vector), "k": k, "num_candidates": max(self.num_candidates, k)}
        if filter:
            knn["filter"] = self._filters(filter)
        if self.rescore_oversample:
            knn["rescore_vector"] = {"oversample": self.rescore_oversample}
        return knn

    def vector_body(self, vector, filter, k: int) -> dict:
        return {"size": k, "knn": self.knn_clause(vector, filter, k)}

    def fused_body(self, query: str, vector, filter, k: int) -> dict:
        std = {"standard": {"query": self.lexical_body(query, filter, k)["query"]}}
        knn = {"knn": self.knn_clause(vector, filter, max(k, self.rank_window))}
        if self.backend_fusion == "linear":
            r = {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"}, {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}],
                            "rank_window_size": self.rank_window}}
        else:
            r = {"rrf": {"retrievers": [std, knn], "rank_window_size": self.rank_window, "rank_constant": 60}}
        return {"size": k, "retriever": r}

    # ---- port
    @staticmethod
    def _hits(res: dict) -> List[Hit]:
        return [Hit(h["_id"], float(h.get("_score") or 0.0), h.get("_source") or {}) for h in res.get("hits", {}).get("hits", [])]

    def lexical(self, index, query, filter=None, k=10, tenant=None):  # noqa: D401
        res = self._req("POST", "/%s/_search" % self.index_name(index, tenant), self.lexical_body(query, filter, k))
        return SearchResult(self._hits(res), "lexical", "none")

    def vector(self, index, vector, filter=None, k=10, tenant=None):
        res = self._req("POST", "/%s/_search" % self.index_name(index, tenant), self.vector_body(vector, filter, k))
        return SearchResult(self._hits(res), "vector", "none")

    def _backend_fused(self, index, query, vector, filter, k, tenant):
        res = self._req("POST", "/%s/_search" % self.index_name(index, tenant), self.fused_body(query, vector, filter, k))
        return SearchResult(self._hits(res), "fused", "backend")

    def _client_fused(self, index, query, vector, filter, k, tenant):
        w = max(k, self.rank_window)
        lex, vec = self.lexical(index, query, filter, w, tenant), self.vector(index, vector, filter, w, tenant)
        by = {h.id: h for h in lex.hits + vec.hits}
        out = rrf([[h.id for h in lex.hits], [h.id for h in vec.hits]])[:k]
        return SearchResult([Hit(i, s, by[i].source) for i, s in out], "fused", "client")

    @staticmethod
    def _is_licence_error(e: Exception) -> bool:
        m = str(e).lower()
        return "license" in m or "licence" in m or "non-compliant" in m

    def capability_probe(self, index, tenant=None) -> Optional[bool]:
        """One tiny retriever query. Only DEFINITE outcomes are cached: success (True, until a later licence error resets it) and a
        licence error (False, re-probed after probe_ttl_s). A transient error (503, timeout, missing index, a 403 that is not a
        licence message) returns None, caches nothing and the caller falls back for that call only."""
        try:
            self._req("POST", "/%s/_search" % self.index_name(index, tenant),
                      {"size": 0, "retriever": {"rrf": {"retrievers": [{"standard": {"query": {"match_all": {}}}}, {"standard": {"query": {"match_all": {}}}}],
                                                        "rank_window_size": 1}}})
            self._probe, self.probe_error, self._probe_at = True, None, time.time()
        except (BackendError, CallTimeout) as e:
            self.probe_error = str(e)[:200]
            if isinstance(e, BackendError) and self._is_licence_error(e):
                self._probe, self._probe_at = False, time.time()
            else:
                self._probe = None
                self.fusion_location = "unknown"
                return None
        self.fusion_location = "backend" if self._probe else "client"
        return bool(self._probe)

    def _probe_state(self, index, tenant) -> Optional[bool]:
        if self._probe is False and time.time() - self._probe_at > self.probe_ttl_s:
            self._probe = None                      # a licence can come back: re-probe after the TTL
        return self._probe if self._probe is not None else self.capability_probe(index, tenant)

    def fused(self, index, query, vector, filter=None, k=10, tenant=None):
        if self.strategy == "client":
            return self._client_fused(index, query, vector, filter, k, tenant)
        if self.strategy == "backend":
            return self._backend_fused(index, query, vector, filter, k, tenant)
        if self._probe_state(index, tenant):
            try:
                return self._backend_fused(index, query, vector, filter, k, tenant)
            except BackendError as e:
                if not self._is_licence_error(e):
                    raise
                self._probe, self._probe_at, self.probe_error = False, time.time(), str(e)[:200]   # licence lapsed mid-run: fall back now
                self.fusion_location = "client"
        return self._client_fused(index, query, vector, filter, k, tenant)

    # ---- helpers for tests / setup (index per tenant)
    def create_index(self, logical: str, dims: int, filter_fields: Sequence[str] = ("group",), tenant: Optional[str] = None) -> None:
        props = {self.text_field: {"type": "text"}, self.vector_field: {"type": "dense_vector", "dims": dims, "similarity": "cosine"},
                 TENANT_FIELD: {"type": "keyword"}}
        props.update({f: {"type": "keyword"} for f in filter_fields})
        self._req("PUT", "/" + self.index_name(logical, tenant), {"mappings": {"properties": props}})

    def bulk_add(self, logical: str, docs: Dict[str, dict], tenant: Optional[str] = None) -> None:
        lines = []
        for i, d in docs.items():
            if d.get(TENANT_FIELD, self.tenant) != self.tenant:
                raise CrossTenantError("document %r names tenant %r" % (i, d.get(TENANT_FIELD)))
            lines += [json.dumps({"index": {"_id": i}}), json.dumps(dict(d, **{TENANT_FIELD: self.tenant}))]
        req = urllib.request.Request("%s/%s/_bulk?refresh=true" % (self.base, self.index_name(logical, tenant)), data=("\n".join(lines) + "\n").encode(), method="POST",
                                     headers={"Content-Type": "application/x-ndjson", "Authorization": self._auth()})
        with _urlopen(req, timeout=self.timeout_s, context=self._ctx) as r:
            out = json.loads(r.read())
        if out.get("errors"):
            raise BackendError("bulk errors: %s" % json.dumps(out)[:300])

    def delete_index(self, logical: str, tenant: Optional[str] = None) -> None:
        self._req("DELETE", "/" + self.index_name(logical, tenant))
