"""Pure payload check (no SDK import, unit-testable anywhere). Payload check: the engine may carry ids, cursors and small counters only. Any workflow input, signal argument,
activity argument or result, or continue-as-new state that holds free text, or is larger than MAX_BYTES, is rejected.

Installed as a Temporal PayloadCodec by engine/codec.py."""
import json
import re

MAX_BYTES = 4096
MAX_LIST = 64
MAX_DEPTH = 4
ID_RE = re.compile(r"^[A-Za-z0-9_.:/=+\-]{0,160}$")  # no whitespace, so no prose
KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class PayloadRejected(ValueError):
    pass


def check_value(v, depth=0):
    if depth > MAX_DEPTH:
        raise PayloadRejected("too deep")
    if v is None or isinstance(v, (bool, int, float)):
        return
    if isinstance(v, str):
        if not ID_RE.match(v):
            raise PayloadRejected(f"string is not an id (len={len(v)})")
        return
    if isinstance(v, list):
        if len(v) > MAX_LIST:
            raise PayloadRejected(f"list too long ({len(v)})")
        for x in v:
            check_value(x, depth + 1)
        return
    if isinstance(v, dict):
        if len(v) > MAX_LIST:
            raise PayloadRejected("dict too large")
        for k, x in v.items():
            if not isinstance(k, str) or not KEY_RE.match(k):
                raise PayloadRejected("bad key")
            check_value(x, depth + 1)
        return
    raise PayloadRejected(f"type {type(v).__name__}")


def check_bytes(data: bytes, encoding: bytes):
    if len(data) > MAX_BYTES:
        raise PayloadRejected(f"payload {len(data)} bytes > {MAX_BYTES}")
    if encoding == b"binary/null":
        return
    if encoding != b"json/plain":
        raise PayloadRejected(f"encoding {encoding!r}")
    check_value(json.loads(data.decode("utf-8")))
