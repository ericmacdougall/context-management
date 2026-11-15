"""Minimal Elasticsearch client (standard library only) with ONE write choke point.

Every document write goes through `bulk_fenced`, which always sends require_alias=true: a write can only address an
alias, never a concrete index name and never a missing alias (no auto-create). Per-epoch write aliases are the fence
(see projector.py). Errors are reduced to type + status before they leave this module, so no response body (which can
quote field values, review N13) is ever stored or logged."""
import http.client
import json
import ssl
import urllib.parse

from . import config


class ESError(Exception):
    def __init__(self, status, etype, what=""):
        super().__init__(f"ES {status} {etype} {what}")
        self.status, self.etype = status, etype


def _etype(j):
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        return e.get("type", "unknown")
    return "unknown" if e else ""


class ES:
    def __init__(self, url=None):
        u = urllib.parse.urlparse(url or config.ES_URL)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.auth = config.es_auth_header()
        self.conn = None
        self.write_requests = 0

    def _c(self):
        if self.conn is None:
            if self.scheme == "https":
                ctx = ssl.create_default_context(cafile=str(config.SECRETS / "http_ca.crt"))
                self.conn = http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=120)
            else:
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        return self.conn

    def req(self, method, path, body=None, ndjson=None, ok=(200, 201), **params):
        if params:
            path += "?" + urllib.parse.urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()})
        hdr = {"Content-Type": "application/json"}
        if self.auth:
            hdr["Authorization"] = self.auth
        if ndjson is not None:
            data, hdr["Content-Type"] = ndjson.encode(), "application/x-ndjson"
        else:
            data = json.dumps(body).encode() if body is not None else None
        for attempt in (0, 1):
            try:
                c = self._c()
                c.request(method, path, body=data, headers=hdr)
                r = c.getresponse()
                raw = r.read()
                break
            except (http.client.HTTPException, OSError):
                self.conn = None
                if attempt:
                    raise
        try:
            j = json.loads(raw) if raw else {}
        except ValueError:
            j = {}
        if ok is not None and r.status not in ok:
            raise ESError(r.status, _etype(j), f"{method} {path.split('?')[0]}")
        return r.status, j

    # ------------------------------------------------------------ the only document-write path
    def bulk_fenced(self, actions, refresh=False):
        """actions: list of (action_dict, source_or_None). Every action must target an alias (require_alias=true).
        Returns (n_ok, [(op, status, error_type)]) for the failed items; the caller decides what a failure means."""
        lines = []
        for a, src in actions:
            (op, meta), = a.items()
            assert "_index" in meta, "bulk action without an explicit target"
            lines.append(json.dumps(a, separators=(",", ":")))
            if src is not None:
                lines.append(json.dumps(src, separators=(",", ":")))
        self.write_requests += 1
        _, j = self.req("POST", "/_bulk", ndjson="\n".join(lines) + "\n", require_alias=True, refresh=refresh)
        bad, n_ok = [], 0
        for it in j.get("items", []):
            (op, r), = it.items()
            st = r.get("status", 500)
            if st < 300:
                n_ok += 1
            else:
                bad.append((op, st, (r.get("error") or {}).get("type", "unknown")))
        return n_ok, bad

    def aliases(self, actions):
        """Atomic alias update (all actions or none)."""
        return self.req("POST", "/_aliases", {"actions": actions})[1]

    def indices(self, pattern):
        s, j = self.req("GET", f"/_cat/indices/{pattern}", ok=(200, 404), format="json", h="index,docs.count", expand_wildcards="all")
        return [r["index"] for r in j] if s == 200 else []

    def alias_map(self, pattern):
        """{index: set(aliases)} for indices matching pattern."""
        s, j = self.req("GET", f"/{pattern}/_alias", ok=(200, 404), expand_wildcards="all")
        return {i: set(v.get("aliases", {})) for i, v in j.items()} if s == 200 else {}

    def count(self, target, query):
        s, j = self.req("POST", f"/{target}/_count", {"query": query}, ok=(200, 404), ignore_unavailable=True, allow_no_indices=True)
        return j.get("count", 0) if s == 200 else 0
