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


class CurlEsClient(EsClient):
    """Same interface as EsClient, but speaks through `curl --config <file>` so a node that needs TLS or
    auth settings kept in a curl config file can be used without this process ever reading or printing
    that file. The file path is the only argument that carries the credential (never its content)."""

    def __init__(self, base_url: str, curl_config: str, timeout: float = 120.0, allow_nonlocal: bool = False):
        super().__init__(base_url, None, timeout=timeout, allow_nonlocal=allow_nonlocal)
        if not os.path.exists(curl_config):
            raise EsError("curl config file not found")
        self._cfg = curl_config

    def __repr__(self) -> str:
        return "CurlEsClient(%r)" % self.base_url

    def request(self, method: str, path: str, body: Any = None, ndjson: bool = False) -> Dict[str, Any]:
        import subprocess
        cmd = ["curl", "--config", self._cfg, "-sS", "-m", str(int(self.timeout)), "-X", method,
               "-H", "Accept: application/json", "-w", "\n%{http_code}", self.base_url + path]
        data = None
        if body is not None:
            if ndjson:
                data = ("\n".join(json.dumps(x) for x in body) + "\n").encode()
            else:
                data = json.dumps(body).encode()
            # Content-Type is NOT sent here: the curl config supplies it (a duplicate header is rejected by
            # Elasticsearch); _bulk accepts application/json bodies.
            cmd[1:1] = ["--data-binary", "@-"]
        try:
            out = subprocess.run(cmd, input=data, capture_output=True, timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            raise EsError("%s %s timed out" % (method, path)) from None
        if out.returncode != 0:
            raise EsError("%s %s: curl exit %d" % (method, path, out.returncode))
        raw, _, code = out.stdout.rpartition(b"\n")
        if not code.strip().startswith(b"2"):
            raise EsError("%s %s -> HTTP %s: %s" % (method, path, code.decode().strip(), raw[:300].decode("utf-8", "replace")))
        return json.loads(raw) if raw.strip() else {}
