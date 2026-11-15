"""Minimal Elasticsearch client on the standard library (no extra dependency).

The scoped API key and CA certificate are read in-process from the host files
and never printed or logged; failures redact the Authorization header."""
import http.client
import json
import os
import pathlib
import ssl
import time
import urllib.parse

H = pathlib.Path(os.environ.get("EMLAB_HOME", os.path.expanduser("~/emlab-16096")))
HOST, PORT = "127.0.0.1", 19200


class ESError(Exception):
    def __init__(self, status, body, what=""):
        super().__init__(f"ES {status} {what}: {str(body)[:300]}")
        self.status, self.body = status, body


class ES:
    def __init__(self, admin=False):
        self.ctx = ssl.create_default_context(cafile=str(H / "http_ca.crt"))
        if admin:
            import base64
            tok = base64.b64encode(b"elastic:" + (H / "es_password").read_bytes().strip()).decode()
            self._auth = "Basic " + tok
        else:
            self._auth = "ApiKey " + json.loads((H / "key.json").read_text())["encoded"]
        self.conn = None

    def _c(self):
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(HOST, PORT, context=self.ctx, timeout=120)
        return self.conn

    def req(self, method, path, body=None, ndjson=None, ok=(200, 201), **params):
        """Returns (status, json). Raises ESError unless status in ok (None = never raise)."""
        if params:
            path += "?" + urllib.parse.urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()})
        hdr = {"Authorization": self._auth, "Content-Type": "application/json"}
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
            j = {"_raw": raw[:500].decode("utf8", "replace")}
        if ok is not None and r.status not in ok:
            raise ESError(r.status, j, f"{method} {path.split('?')[0]}")
        return r.status, j

    def bulk(self, lines, **params):
        """lines: list of dicts (action, source, ...). Returns the parsed response; caller checks items."""
        nd = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines)
        return self.req("POST", "/_bulk", ndjson=nd, **params)[1]


def bulk_errors(resp, tolerate=(409,)):
    """Items whose status is an error other than tolerated ones."""
    bad = []
    if resp.get("errors"):
        for it in resp["items"]:
            (op, r), = it.items()
            if r.get("status", 200) >= 300 and r["status"] not in tolerate:
                bad.append((op, r.get("_index"), r.get("_id"), r.get("status"), str(r.get("error"))[:200]))
    return bad
