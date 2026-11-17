"""Canonical JSON and keyed hashing. Hashes of content use HMAC with a per-tenant key so a digest of a low-entropy secret is
not confirmable by an attacker who reads the log (review note: plain SHA-256 of pre-redaction content is a confirmation oracle)."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def cj(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Hasher:
    """tenant_key is supplied by the trusted caller (key store); the tap never derives it from content."""

    def __init__(self, tenant_key: bytes):
        if not tenant_key:
            raise ValueError("tenant_key required")
        self.key = tenant_key

    def h(self, s: str) -> str:
        return hmac.new(self.key, s.encode("utf-8"), hashlib.sha256).hexdigest()

    def hj(self, obj: Any) -> str:
        return self.h(cj(obj))
