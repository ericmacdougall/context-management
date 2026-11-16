"""Secret scanner INTERFACE plus a reference implementation used only by the tests. The real scanner is the client's; the tap
depends on `Scanner.scan` only. The reference decodes base64, hex, percent-encoding and unicode escapes before matching."""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from .canon import Hasher


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    kind: str


class Scanner:
    def scan(self, text: str) -> List[Finding]:
        raise NotImplementedError


class SecretBlocked(Exception):
    """Policy 'block': a secret was found in an outbound request; nothing is sent (the log gets a tombstone)."""


_PATTERNS = [
    ("api_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password_kv", re.compile(r"(?i)(?:password|passwd|secret)\s*[:=]\s*\S{6,}")),
]
_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}){12,}")
_PCT = re.compile(r"(?:%[0-9a-fA-F]{2}[A-Za-z0-9._~-]{0,3}){6,}")
_ESC = re.compile(r"(?:\\{1,2}u[0-9a-fA-F]{4}|\\{1,2}x[0-9a-fA-F]{2}){6,}")


def _direct(t: str) -> Optional[str]:
    for kind, rx in _PATTERNS:
        if rx.search(t):
            return kind
    return None


def _decode_variants(tok: str) -> List[str]:
    out = []
    try:
        pad = tok + "=" * (-len(tok) % 4)
        out.append(base64.b64decode(pad.replace("-", "+").replace("_", "/"), validate=False).decode("utf-8", "ignore"))
    except (binascii.Error, ValueError):
        pass
    try:
        out.append(bytes.fromhex(tok).decode("utf-8", "ignore"))
    except ValueError:
        pass
    out.append(urllib.parse.unquote(tok))
    try:
        out.append(tok.replace("\\\\", "\\").encode("ascii").decode("unicode_escape"))
    except (UnicodeError, ValueError):
        pass
    return out


_ESC_FMT = re.compile(r"\\{1,2}u(?:200[b-fB-F]|2060|feff|FEFF)")


def _norm(s: str) -> str:
    s = _ESC_FMT.sub("", s)                    # JSON-escaped zero-width characters (a block that was JSON-encoded)
    s = unicodedata.normalize("NFKC", s)
    return "".join(c for c in s if unicodedata.category(c) != "Cf")


class ReferenceScanner(Scanner):
    def __init__(self, max_depth: int = 3):
        self.max_depth = max_depth

    def _encoded(self, text: str, depth: int) -> Optional[str]:
        if depth > self.max_depth:
            return None
        for rx in (_B64, _HEX, _PCT, _ESC):
            for m in rx.finditer(text):
                for dec in _decode_variants(m.group(0)):
                    if not dec:
                        continue
                    k = _direct(dec) or _direct(_norm(dec)) or self._encoded(dec, depth + 1)
                    if k:
                        return "encoded_" + k
        return None

    def scan(self, text: str) -> List[Finding]:
        out: List[Finding] = []
        for kind, rx in _PATTERNS:
            for m in rx.finditer(text):
                out.append(Finding(m.start(), m.end(), kind))
        for rx in (_B64, _HEX, _PCT, _ESC):
            for m in rx.finditer(text):
                for dec in _decode_variants(m.group(0)):
                    k = dec and (_direct(dec) or _direct(_norm(dec)) or self._encoded(dec, 1))
                    if k:
                        out.append(Finding(m.start(), m.end(), k if k.startswith("encoded_") else "encoded_" + k))
                        break
        n = _norm(text)
        if n != text and (_direct(n) or self._encoded(n, 0)) and not out:
            out.append(Finding(0, len(text), "obfuscated_whole_string"))   # cannot map spans: fail closed, redact the string
        return out


def redact_text(text: str, scanner: Scanner, hasher: Hasher) -> str:
    fs = sorted(scanner.scan(text), key=lambda f: (f.start, -f.end))
    if not fs:
        return text
    out, pos = [], 0
    for f in fs:
        if f.start < pos:
            if f.end > pos:       # overlapping: extend the previous redaction instead
                pos = f.end
            continue
        out.append(text[pos:f.start])
        out.append("[REDACTED:%s:%s]" % (f.kind, hasher.h(text[f.start:f.end])[:8]))
        pos = f.end
    out.append(text[pos:])
    return "".join(out)


# Provider-opaque fields that must be stored verbatim (mutating them breaks the provider's integrity check). They may carry
# the secret in encrypted form; that is stated in the report, not hidden.
VERBATIM_KEYS = frozenset(["signature", "data"])


def redact_obj(obj: Any, scanner: Scanner, hasher: Hasher, counter: Optional[list] = None, _key: str = "", _parent_type: str = "") -> Any:
    if isinstance(obj, str):
        if _key in VERBATIM_KEYS and _parent_type in ("thinking", "redacted_thinking"):
            return obj
        r = redact_text(obj, scanner, hasher)
        if counter is not None and r != obj:
            counter.append(1)
        return r
    if isinstance(obj, dict):
        t = obj.get("type", "") if isinstance(obj.get("type"), str) else ""
        return {k: redact_obj(v, scanner, hasher, counter, k, t) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, scanner, hasher, counter, _key, _parent_type) for v in obj]
    return obj
