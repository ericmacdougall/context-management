"""Replicate stream: shared helpers (ES clients with a write guard, stamping, environment fingerprint).

Secrets are read from files on the host and kept in memory only; nothing here prints them.
Shared node (trial, owner lab): read only, except paths under the `rep16096-` prefix.
Own node (emlab-es-rep-16096): anything.
"""
import hashlib
import json
import os
import platform
import re
import socket
import sys
import time

import requests

H = os.path.expanduser("~/emlab-16096")
REP = os.path.join(H, "rep")
RESULTS = os.environ.get("REP_RESULTS", os.path.join(REP, "results"))
THRESHOLDS_COMMIT = "8c3b5e6"
PREFIX = "rep16096-"


class GuardError(Exception):
    pass


_READ_POST = re.compile(r"^/(?:[^/]+/)?(?:_search|_count|_msearch|_explain/[^/]+|_field_caps|_analyze|_validate/query)$")


def _shared_allowed(method, path):
    p = path.split("?")[0]
    if p.startswith("/" + PREFIX) or p.startswith("/_index_template/" + PREFIX) or p.startswith("/_data_stream/" + PREFIX):
        return True
    if method in ("GET", "HEAD"):
        return True
    if method == "POST" and (_READ_POST.match(p) or p in ("/_query",)):
        # ES|QL /_query is read only (no write commands exist in ES|QL)
        return True
    return False


class Es:
    def __init__(self, name, url, session, guard=False):
        self.name, self.url, self.s, self.guard = name, url, session, guard

    def req(self, method, path, body=None, ndjson=None, timeout=300, **params):
        if self.guard and not _shared_allowed(method, path):
            raise GuardError("refused %s %s on shared node" % (method, path))
        kw = {"params": params, "timeout": timeout}
        if ndjson is not None:
            kw["data"] = ndjson
            kw["headers"] = {"Content-Type": "application/x-ndjson"}
        elif body is not None:
            kw["data"] = json.dumps(body)
            kw["headers"] = {"Content-Type": "application/json"}
        t = time.perf_counter()
        r = self.s.request(method, self.url + path, **kw)
        dt = (time.perf_counter() - t) * 1000
        try:
            j = r.json()
        except Exception:
            j = {"_raw": r.text[:2000]}
        return r.status_code, j, dt


def shared_es():
    k = json.load(open(os.path.join(H, "key.json")))["encoded"]
    s = requests.Session()
    s.verify = os.path.join(H, "http_ca.crt")
    s.headers["Authorization"] = "ApiKey " + k
    return Es("shared-emlab-es-16096", "https://127.0.0.1:19200", s, guard=True)


def rep_es():
    pw = open(os.path.join(REP, "secrets", "es_pw")).read().strip()
    s = requests.Session()
    s.auth = ("elastic", pw)
    return Es("own-emlab-es-rep-16096", "http://127.0.0.1:19280", s, guard=False)


def es_fingerprint(es, index=None):
    out = {"node": es.name}
    c, j, _ = es.req("GET", "/")
    out["version"] = j.get("version", {}).get("number")
    out["build_hash"] = j.get("version", {}).get("build_hash")
    c, j, _ = es.req("GET", "/_license")
    lic = j.get("license", {})
    out["licence"] = {"type": lic.get("type"), "uid": lic.get("uid"), "status": lic.get("status"),
                      "expiry_date": lic.get("expiry_date")}
    c, j, _ = es.req("GET", "/_cluster/health")
    out["nodes"] = j.get("number_of_nodes")
    if index:
        c, j, _ = es.req("GET", "/%s/_count" % index)
        out["index"] = {"name": index, "docs": j.get("count")}
    return out


def host_fingerprint():
    try:
        la = os.getloadavg()
    except OSError:
        la = None
    return {"host": socket.gethostname(), "loadavg_1_5_15": la, "cpus": os.cpu_count(),
            "python": sys.version.split()[0], "platform": platform.platform()}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def stamp(script_path, params, extra=None):
    return {"thresholds_commit": THRESHOLDS_COMMIT,
            "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
            "script": os.path.basename(script_path), "script_sha256": sha256_file(script_path),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": params,
            "host": host_fingerprint(), **(extra or {})}


def save(name, obj):
    os.makedirs(RESULTS, exist_ok=True)
    p = os.path.join(RESULTS, name)
    with open(p, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    return p


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (c - h, c + h)
