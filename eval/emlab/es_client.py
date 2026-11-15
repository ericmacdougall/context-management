"""Minimal Elasticsearch client on urllib. Not used by --dry-run.

Security rules: the credential comes only from a FILE PATH argument (never argv value, never an env var);
the file must not be group/world readable; the base URL must be loopback unless allow_nonlocal=True
(runs bind localhost only and reach the node through an SSH tunnel); the credential never appears in
errors or reprs.
"""
from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class EsError(RuntimeError):
    pass


def read_credential_file(path: str) -> str:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise EsError("credential file %s must not be group/world accessible (chmod 600)" % path)
    with open(path, "r", encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        raise EsError("credential file is empty")
    return value


class EsClient:
    def __init__(self, base_url: str, credential_file: Optional[str] = None, scheme: str = "ApiKey",
                 timeout: float = 30.0, allow_nonlocal: bool = False,
                 opener: Optional[Callable[[urllib.request.Request, float], Any]] = None):
        u = urllib.parse.urlparse(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise EsError("base_url must be http(s)://host:port")
        if u.hostname not in LOOPBACK and not allow_nonlocal:
            raise EsError("refusing non-loopback host %r (pass allow_nonlocal=True to override)" % u.hostname)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth: Optional[str] = None
        if credential_file:
            self._auth = "%s %s" % (scheme, read_credential_file(credential_file))
        self._opener = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))

    def __repr__(self) -> str:
        return "EsClient(%r, auth=%s)" % (self.base_url, "set" if self._auth else "none")

    def request(self, method: str, path: str, body: Any = None, ndjson: bool = False) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            if ndjson:
                data = ("\n".join(json.dumps(x) for x in body) + "\n").encode()
                headers["Content-Type"] = "application/x-ndjson"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            resp = self._opener(req, self.timeout)
            raw = resp.read()
        except urllib.error.HTTPError as e:
            raise EsError("%s %s -> HTTP %s" % (method, path, e.code)) from None
        except urllib.error.URLError as e:
            raise EsError("%s %s failed: %s" % (method, path, e.reason)) from None
        return json.loads(raw) if raw else {}

    def info(self) -> Dict[str, Any]:
        return self.request("GET", "/")

    def create_index(self, name: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PUT", "/" + urllib.parse.quote(name), body)

    def delete_index(self, name: str) -> Dict[str, Any]:
        return self.request("DELETE", "/" + urllib.parse.quote(name))

    def bulk(self, index: str, docs: Iterable[Dict[str, Any]], id_field: str = "doc_id") -> Dict[str, Any]:
        lines: List[Dict[str, Any]] = []
        for d in docs:
            lines.append({"index": {"_index": index, "_id": d[id_field]}})
            lines.append(d)
        out = self.request("POST", "/_bulk", lines, ndjson=True)
        if out.get("errors"):
            raise EsError("bulk reported item errors")
        return out

    def refresh(self, index: str) -> Dict[str, Any]:
        return self.request("POST", "/%s/_refresh" % urllib.parse.quote(index))

    def search(self, index: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/%s/_search" % urllib.parse.quote(index), body)
