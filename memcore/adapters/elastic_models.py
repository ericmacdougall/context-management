"""Embedder and Reranker over Elasticsearch inference endpoints (text_embedding, rerank). stdlib urllib.
The Embedder maps the port's kind to the endpoint's input_type ("search" for query, "ingest" for passage) and ALWAYS sends it,
so the E5 prefix is applied by the endpoint, never twice and never forgotten (port-level prefixes are empty here)."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Sequence

from ..http_util import urlopen as _urlopen
from ..ports import BackendError, Embedder, Reranker, RerankHit

INPUT_TYPE = {"query": "search", "passage": "ingest"}


class _Es:
    def __init__(self, base_url, auth_header, ca_file=None, verify_tls=True, timeout_s=120.0):
        self.base, self._auth, self.timeout_s, self._ctx = base_url.rstrip("/"), auth_header, timeout_s, None
        if base_url.startswith("https"):
            self._ctx = ssl.create_default_context(cafile=ca_file)
            if not verify_tls:
                self._ctx.check_hostname, self._ctx.verify_mode = False, ssl.CERT_NONE

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": self._auth()})
        try:
            with _urlopen(req, timeout=self.timeout_s, context=self._ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise BackendError("HTTP %d: %s" % (e.code, e.read()[:300].decode("utf-8", "replace"))) from None


class ElasticInferenceEmbedder(Embedder):
    def __init__(self, base_url, endpoint_id, auth_header, dimensions: int, model: Optional[str] = None, **kw):
        self.es, self.endpoint_id, self.dimensions, self.model = _Es(base_url, auth_header, **kw), endpoint_id, dimensions, model or endpoint_id

    def _embed(self, texts, kind):
        r = self.es.post("/_inference/text_embedding/%s" % self.endpoint_id, {"input": texts, "input_type": INPUT_TYPE[kind]})
        vecs = [d["embedding"] for d in r["text_embedding"]]
        if len(vecs) != len(texts) or any(len(v) != self.dimensions for v in vecs):
            raise BackendError("unexpected embedding shape from %s" % self.endpoint_id)
        return vecs


class ElasticInferenceReranker(Reranker):
    def __init__(self, base_url, endpoint_id, auth_header, **kw):
        self.es, self.endpoint_id, self.model = _Es(base_url, auth_header, **kw), endpoint_id, endpoint_id

    def rerank(self, query, documents, top_n=None):
        body = {"query": query, "input": list(documents)}
        if top_n:
            body["top_n"] = top_n
        r = self.es.post("/_inference/rerank/%s" % self.endpoint_id, body)
        hits = [RerankHit(int(x["index"]), float(x["relevance_score"])) for x in r["rerank"]]
        return sorted(hits, key=lambda h: (-h.score, h.index))
