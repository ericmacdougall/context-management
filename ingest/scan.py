"""Secret scanner INTERFACE plus a reference implementation used only by the tests. The real scanner is the client's; the tap
depends on `Scanner.scan` only. The reference decodes base64, hex, percent-encoding and unicode escapes before matching."""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from functools import lru_cache
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


_PEM_BLOCK = (r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
              r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|(?:[ \t]*\r?\n)?(?:[A-Za-z0-9+/=]{8,}[ \t]*\r?\n?)*)")
_PATTERNS = [
    ("api_key", re.compile(r"sk[-_][A-Za-z0-9_-]{16,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("private_key", re.compile(_PEM_BLOCK)),                     # the whole block (header, body, footer); with no footer: header plus the base64 body that follows
    ("password_kv", re.compile(r"(?i)(?:password|passwd|secret)\s*[:=]\s*\S{6,}")),
    ("vendor_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")),
    ("aws_secret", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S{16,}")),
    ("url_credentials", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{4,}@")),
]
_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}){12,}")
_PCT = re.compile(r"(?:%[0-9a-fA-F]{2}[A-Za-z0-9._~-]{0,3}){6,}")
_ESC = re.compile(r"(?:\\{1,2}u[0-9a-fA-F]{4}|\\{1,2}x[0-9a-fA-F]{2}){6,}")
_B32 = re.compile(r"[A-Za-z2-7]{16,}={0,6}")
_QP = re.compile(r"(?:=[0-9A-Fa-f]{2}){6,}")
_HTML = re.compile(r"(?:&#(?:[xX][0-9a-fA-F]+|[0-9]+);){6,}")
_OCT = re.compile(r"(?:\\[0-7]{3}){6,}")
_SPLIT = re.compile(r"(?:[^\s][-._ \t]){12,}[^\s]")                  # one separator between single characters
_WRAP = re.compile(r"(?:[A-Za-z0-9+/_-]{8,}[ \t]*\r?\n)+[A-Za-z0-9+/_-]{4,}={0,2}")        # an encoded token broken over lines
_TOKENS = (_B64, _HEX, _PCT, _ESC, _B32, _QP, _HTML, _OCT, _SPLIT, _WRAP)


def _direct(t: str) -> Optional[str]:
    for kind, rx in _PATTERNS:
        if rx.search(t):
            return kind
    return None


def _decode_variants(tok: str) -> List[str]:
    out = []
    squeezed = re.sub(r"\s+", "", tok)
    try:
        pad = squeezed + "=" * (-len(squeezed) % 4)
        out.append(base64.b64decode(pad.replace("-", "+").replace("_", "/"), validate=False).decode("utf-8", "ignore"))
    except (binascii.Error, ValueError):
        pass
    try:
        out.append(bytes.fromhex(squeezed).decode("utf-8", "ignore"))
    except ValueError:
        pass
    out.append(urllib.parse.unquote(tok))
    try:
        out.append(tok.replace("\\\\", "\\").encode("ascii").decode("unicode_escape"))
    except (UnicodeError, ValueError):
        pass
    try:
        b = squeezed.upper()
        out.append(base64.b32decode(b + "=" * (-len(b) % 8)).decode("utf-8", "ignore"))
    except (binascii.Error, ValueError):
        pass
    if "=" in tok:
        try:
            out.append(re.sub(r"=([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), tok))
        except ValueError:
            pass
    if "&#" in tok:
        try:
            out.append(re.sub(r"&#([xX][0-9a-fA-F]+|[0-9]+);", lambda m: chr(int(m.group(1)[1:], 16) if m.group(1)[0] in "xX" else int(m.group(1))), tok))
        except (ValueError, OverflowError):
            pass
    if "\\" in tok:
        out.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), tok))
    out.append(tok[::2])                                  # one separator between every character
    out.append(re.sub(r"[-._ \t]", "", tok))
    return out


_ESC_FMT = re.compile(r"\\{1,2}u(?:200[b-fB-F]|2060|feff|FEFF)")


def _norm(s: str) -> str:
    s = _ESC_FMT.sub("", s)                    # JSON-escaped zero-width characters (a block that was JSON-encoded)
    s = unicodedata.normalize("NFKC", s)
    return "".join(c for c in s if unicodedata.category(c) != "Cf")


@lru_cache(maxsize=16384)
def _token_kind(tok: str, depth: int, max_depth: int) -> Optional[str]:
    """First decoding of `tok` that exposes a credential pattern (memoised: PERFORMANCE ONLY, same result as the unmemoised loop)."""
    for dec in _decode_variants(tok):
        if not dec:
            continue
        k = _direct(dec) or _direct(_norm(dec)) or _encoded_text(dec, depth, max_depth)
        if k:
            return k
    return None


@lru_cache(maxsize=16384)
def _encoded_text(text: str, depth: int, max_depth: int) -> Optional[str]:
    if depth > max_depth:
        return None
    for rx in _TOKENS:
        for m in rx.finditer(text):
            k = _token_kind(m.group(0), depth + 1, max_depth)
            if k:
                return "encoded_" + k
    return None


class ReferenceScanner(Scanner):
    def __init__(self, max_depth: int = 3):
        self.max_depth = max_depth

    def _encoded(self, text: str, depth: int) -> Optional[str]:
        return _encoded_text(text, depth, self.max_depth)

    def scan(self, text: str) -> List[Finding]:
        raise NotImplementedError


class SecretBlocked(Exception):
    """Policy 'block': a secret was found in an outbound request; nothing is sent (the log gets a tombstone)."""


_PEM_BLOCK = (r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
              r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|(?:[ \t]*\r?\n)?(?:[A-Za-z0-9+/=]{8,}[ \t]*\r?\n?)*)")
_PATTERNS = [
    ("api_key", re.compile(r"sk[-_][A-Za-z0-9_-]{16,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("private_key", re.compile(_PEM_BLOCK)),                     # the whole block (header, body, footer); with no footer: header plus the base64 body that follows
    ("password_kv", re.compile(r"(?i)(?:password|passwd|secret)\s*[:=]\s*\S{6,}")),
    ("vendor_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")),
    ("aws_secret", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S{16,}")),
    ("url_credentials", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{4,}@")),
]
_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}){12,}")
_PCT = re.compile(r"(?:%[0-9a-fA-F]{2}[A-Za-z0-9._~-]{0,3}){6,}")
_ESC = re.compile(r"(?:\\{1,2}u[0-9a-fA-F]{4}|\\{1,2}x[0-9a-fA-F]{2}){6,}")
_B32 = re.compile(r"[A-Za-z2-7]{16,}={0,6}")
_QP = re.compile(r"(?:=[0-9A-Fa-f]{2}){6,}")
_HTML = re.compile(r"(?:&#(?:[xX][0-9a-fA-F]+|[0-9]+);){6,}")
_OCT = re.compile(r"(?:\\[0-7]{3}){6,}")
_SPLIT = re.compile(r"(?:[^\s][-._ \t]){12,}[^\s]")                  # one separator between single characters
_WRAP = re.compile(r"(?:[A-Za-z0-9+/_-]{8,}[ \t]*\r?\n)+[A-Za-z0-9+/_-]{4,}={0,2}")        # an encoded token broken over lines
_TOKENS = (_B64, _HEX, _PCT, _ESC, _B32, _QP, _HTML, _OCT, _SPLIT, _WRAP)


def _direct(t: str) -> Optional[str]:
    for kind, rx in _PATTERNS:
        if rx.search(t):
            return kind
    return None


def _decode_variants(tok: str) -> List[str]:
    out = []
    squeezed = re.sub(r"\s+", "", tok)
    try:
        pad = squeezed + "=" * (-len(squeezed) % 4)
        out.append(base64.b64decode(pad.replace("-", "+").replace("_", "/"), validate=False).decode("utf-8", "ignore"))
    except (binascii.Error, ValueError):
        pass
    try:
        out.append(bytes.fromhex(squeezed).decode("utf-8", "ignore"))
    except ValueError:
        pass
    out.append(urllib.parse.unquote(tok))
    try:
        out.append(tok.replace("\\\\", "\\").encode("ascii").decode("unicode_escape"))
    except (UnicodeError, ValueError):
        pass
    try:
        b = squeezed.upper()
        out.append(base64.b32decode(b + "=" * (-len(b) % 8)).decode("utf-8", "ignore"))
    except (binascii.Error, ValueError):
        pass
    if "=" in tok:
        try:
            out.append(re.sub(r"=([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), tok))
        except ValueError:
            pass
    if "&#" in tok:
        try:
            out.append(re.sub(r"&#([xX][0-9a-fA-F]+|[0-9]+);", lambda m: chr(int(m.group(1)[1:], 16) if m.group(1)[0] in "xX" else int(m.group(1))), tok))
        except (ValueError, OverflowError):
            pass
    if "\\" in tok:
        out.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), tok))
    out.append(tok[::2])                                  # one separator between every character
    out.append(re.sub(r"[-._ \t]", "", tok))
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
        for rx in _TOKENS:
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
        for rx in _TOKENS:
            for m in rx.finditer(text):
                k = _token_kind(m.group(0), 1, self.max_depth)
                if k:
                    out.append(Finding(m.start(), m.end(), k if k.startswith("encoded_") else "encoded_" + k))
        rev = text[::-1]
        for kind, rx in _PATTERNS:
            if kind in ("private_key", "url_credentials", "jwt"):
                continue
            for m in rx.finditer(rev):
                out.append(Finding(len(text) - m.end(), len(text) - m.start(), "reversed_" + kind))
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
