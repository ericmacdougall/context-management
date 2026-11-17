"""Shared helpers for the licence rehearsal (runs on t2-dev). stdlib only. Never prints the password: it is read from a 0600 file."""
import base64
import hashlib
import json
import math
import os
import socket
import time
import urllib.error
import urllib.request

import evidence

BASE = os.environ.get("LIC_ES", "http://127.0.0.1:19270")
PWFILE = os.environ.get("LIC_PW", os.path.expanduser("~/emlab-16096/lic/es_password"))
CODE_COMMIT = os.environ.get("LIC_COMMIT", "unknown")


def _auth(user="elastic", pwfile=None):
    with open(pwfile or PWFILE) as fh:
        return "Basic " + base64.b64encode(("%s:%s" % (user, fh.read().strip())).encode()).decode()


def req(method, path, body=None, ndjson=None, timeout=60, user="elastic", pwfile=None, base=None):
    """-> (status, parsed json or {'_raw': text}, elapsed_ms). status 0 = transport failure (refused / timeout)."""
    data, ct = None, "application/json"
    if ndjson is not None:
        data, ct = ndjson.encode(), "application/x-ndjson"
    elif body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request((base or BASE) + path, method=method, data=data,
                               headers={"Content-Type": ct, "Authorization": _auth(user, pwfile)})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw, code = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        return 0, {"_transport_error": type(e).__name__}, (time.time() - t) * 1000
    try:
        j = json.loads(raw or b"{}")
    except ValueError:
        j = {"_raw": raw[:300].decode("utf-8", "replace")}
    return code, j, (time.time() - t) * 1000


def licence():
    c, j, _ = req("GET", "/_license")
    l = j.get("license") or {}
    return {"status_code": c, "type": l.get("type"), "status": l.get("status"), "uid": l.get("uid"),
            "expiry_ms": l.get("expiry_date_in_millis")}


def err(j):
    e = j.get("error") if isinstance(j, dict) else None
    if not isinstance(e, dict):
        return {"raw": str(e)[:200]} if e else None
    rc = (e.get("root_cause") or [{}])[0]
    return {"type": e.get("type"), "reason": (e.get("reason") or "")[:240], "root": (rc.get("reason") or "")[:240],
            "feature": e.get("license.expired.feature")}


_CELL_CLASS = {"SUCCESS": "AVAILABLE", "TRANSPORT_FAILURE": "TRANSPORT", "LICENCE_REFUSAL": "LICENCE_ERROR"}


def classify(code, j):
    """Matrix cell class from the same closed outcome cases the verdicts use (evidence.Outcome): only a 2xx is AVAILABLE; a 1xx or 3xx is
    OTHER_ERROR (UNEXPECTED); LICENCE_ERROR is only a LICENCE_REFUSAL (a 403 with the licence text: review 8 F13,
    a 503 with the text is an infrastructure failure); a status outside 0 and 100..599 is OTHER_ERROR rather than a crash in a recording helper."""
    try:
        o = evidence.Outcome.of(code, err(j))
    except evidence.Unparsed:
        return "OTHER_ERROR"
    return _CELL_CLASS.get(o.case, "OTHER_ERROR")


def succeeded(code):
    """True only for the SUCCESS outcome case (2xx); recording helpers use this instead of comparing raw statuses."""
    try:
        return evidence.Outcome.of(code).case == "SUCCESS"
    except evidence.Unparsed:
        return False


def cell(code, j, witness=None):
    c = {"class": classify(code, j), "status": code}
    if c["class"] != "AVAILABLE":
        c["error"] = err(j)
    elif witness is not None:
        c["witness"] = witness
    return c


def stamp(params=None):
    try:
        v = req("GET", "/")[1].get("version", {}).get("number")
    except Exception:  # noqa: BLE001
        v = None
    return {"code_commit": CODE_COMMIT, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "es_version": v,
            "licence_read_back": licence(), "host": socket.gethostname(), "loadavg": os.getloadavg(), "nodes": 1, "params": params or {}}


def save(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class BulkError(RuntimeError):
    """A bulk request that cannot be trusted to have written every submitted operation."""


def _bulk_request(index, docs, refresh, op):
    nd = "".join(json.dumps({op: {"_index": index, "_id": i}}) + "\n" + json.dumps(s if op == "index" else {"doc": s}) + "\n" for i, s in docs)
    return req("POST", "/_bulk?refresh=" + refresh, ndjson=nd, timeout=120)


def bulk(index, docs, refresh="true", op="index"):
    """docs: list of (id, source). -> (status, summary). Recording helper: it never raises. `failed` counts every submitted operation that
    did not get a successful item result, so a transport failure, an error body or a truncated `items` list is failed, not zero (review: F2)."""
    c, j, _ = _bulk_request(index, docs, refresh, op)
    items = j.get("items") if isinstance(j, dict) else None
    items = items if isinstance(items, list) else []
    st = [list(it.values())[0] if isinstance(it, dict) and it else {} for it in items]
    ok = sum(1 for s in st if isinstance(s, dict) and succeeded(s.get("status"))) if c == 200 else 0
    first = next((s.get("error") for s in st if isinstance(s, dict) and not succeeded(s.get("status", 500))), None)
    if first is None and ok < len(docs):
        first = {"type": "bulk_incomplete", "reason": "HTTP %s, %d item results for %d operations" % (c, len(items), len(docs))}
    return c, {"items": len(items), "ok": ok, "failed": len(docs) - ok, "first_error": first}


def bulk_strict(index, docs, refresh="true", op="index"):
    """Like bulk, but raises BulkError unless HTTP 200, `errors` is not true, and there is exactly one successful item result per submitted
    operation, in order, for the submitted id."""
    c, j, _ = _bulk_request(index, docs, refresh, op)
    if c != 200 or not isinstance(j, dict):
        raise BulkError("bulk into %s: HTTP %s" % (index, c))
    items = j.get("items")
    if not isinstance(items, list) or len(items) != len(docs):
        raise BulkError("bulk into %s: %s item results for %d operations" % (index, len(items) if isinstance(items, list) else "no", len(docs)))
    if j.get("errors") is True:
        raise BulkError("bulk into %s: errors=true" % index)
    for (i, _s), it in zip(docs, items):
        r = list(it.values())[0] if isinstance(it, dict) and len(it) == 1 else None
        if not isinstance(r, dict) or not succeeded(r.get("status")) or r.get("_id") != i:
            raise BulkError("bulk into %s: bad item result for id %s: %s" % (index, i, json.dumps(r)[:160]))
    return c, {"items": len(items), "ok": len(items), "failed": 0, "first_error": None}


def embed(text, dims=8):
    """Deterministic stub embedder (no model): sha256-seeded unit vector."""
    h = hashlib.sha256(text.encode()).digest()
    v = [(h[i % 32] - 127.5) / 127.5 for i in range(dims)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
