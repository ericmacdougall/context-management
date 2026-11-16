"""Row 19: secret and credential scanning BEFORE anything is sealed and appended (events, inbox payloads, job results).

Deterministic, local, no model calls. A hit refuses the write with SecretDetected, which names the detector and the
position of the offending item, never the matched text. The store refuses an event whose `secret_scan` column is not
the current scanner version (WPS21), so an app build without this scanner (or an older one) cannot write; this is an
attestation by the app, like the mention rescan (the store cannot see plaintext: content is encrypted app-side).

Pipeline (VERSION s1):
  1. views: the text itself; the text with zero-width/format characters removed; with all whitespace removed (catches a
     key split across lines or spaces); unicode-escape and percent decoding; then, up to depth 2, every base64 /
     base64url / hex run of >= 16 characters that decodes to mostly printable text is decoded and scanned again.
  2. detectors (format-specific; the generic one requires a quoted, high-entropy literal after a secret-like name).
  3. allowlist: published documentation examples (e.g. AKIAIOSFODNN7EXAMPLE) are not secrets.
Documented misses (not claimed): reversed or ROT13 text, a secret split across two writes, encrypted or compressed
blobs, secrets with no recognisable format and no secret-like name next to them, natural-language spelled-out keys."""
import base64
import binascii
import math
import re
import urllib.parse

VERSION = "s1"

DETECTORS = [
    ("aws_access_key_id", re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("aws_secret_key", re.compile(r"(?i)aws.{0,20}?(secret|sk).{0,20}?['\"=:\s]([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])(gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,90})")),
    ("slack_token", re.compile(r"xox[abposr]-[0-9A-Za-z-]{10,}")),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9])(sk|rk)_(live|test)_[0-9A-Za-z]{20,}")),
    ("llm_api_key", re.compile(r"(?<![A-Za-z0-9])sk-(ant-[A-Za-z0-9_-]{20,}|proj-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{40,})")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    ("private_key_block", re.compile(r"-----\s*BEGIN\s*(?:RSA\s*|EC\s*|DSA\s*|OPENSSH\s*|ENCRYPTED\s*)?PRIVATE\s*KEY\s*-----")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@]{1,64}:([^\s:/@]{6,128})@[^\s/]+")),
    ("generic_assignment", re.compile(r"(?i)(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)"
                                      r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,200})[\"']")),
]
ALLOW = {"AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
PLACEHOLDER = re.compile(r"(?i)^(x+|\*+|<[^>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|changeme|password|secret|example\w*|your[_-]?\w+|redacted|none|null|\.+)$")
ZW = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")
B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
HEX = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")
UESC = re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}")


class SecretDetected(Exception):
    """A write carried something that looks like a credential. Message: detector names and item index only."""


def _entropy(s):
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _printable(b):
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    ok = sum(1 for ch in t if ch.isprintable() or ch in "\n\r\t")
    return t if t and ok / len(t) >= 0.9 else None


def _decoded(text):
    out = []
    for m in B64.finditer(text):
        tok = m.group(0)
        for dec in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                t = _printable(dec(tok + "=" * (-len(tok) % 4)))
            except (binascii.Error, ValueError):
                t = None
            if t:
                out.append(t)
                break
    for m in HEX.finditer(text):
        t = _printable(bytes.fromhex(m.group(0)))
        if t:
            out.append(t)
    return out


def _views(text, depth=2, decode=True):
    base = [text]
    z = ZW.sub("", text)
    base.append(z)
    base.append(re.sub(r"\s+", "", z))
    if decode:
        if UESC.search(z):
            try:
                base.append(z.encode("latin-1", "backslashreplace").decode("unicode_escape"))
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        if "%" in z:
            base.append(urllib.parse.unquote(z))
    views = list(dict.fromkeys(base))
    if decode and depth > 0:
        for v in list(views):
            for d in _decoded(v):
                views.extend(_views(d, depth - 1, decode))
    return views


def _hits(view):
    found = set()
    for name, rx in DETECTORS:
        for m in rx.finditer(view):
            whole = m.group(0)
            if whole in ALLOW or any(a in whole for a in ALLOW):
                continue
            if name in ("generic_assignment", "aws_secret_key", "url_credentials"):
                val = m.group(m.lastindex)
                if PLACEHOLDER.match(val) or _entropy(val) < 3.0 or not (re.search(r"\d", val) or re.search(r"[^A-Za-z0-9]", val)):
                    continue
            found.add(name)
    return found


def scan(text, decode=True):
    """Set of detector names that fire on any view of `text` (empty = clean)."""
    found = set()
    for v in _views(text, decode=decode):
        found |= _hits(v)
    return found


def check(texts, where="event"):
    """Raise SecretDetected if any text carries a secret; returns VERSION (stored with the row) otherwise."""
    bad = [(i, sorted(f)) for i, f in ((i, scan(t)) for i, t in enumerate(texts)) if f]
    if bad:
        raise SecretDetected(f"{where}: secret-like content refused at items {[i for i, _ in bad]} detectors {sorted({d for _, f in bad for d in f})}")
    return VERSION
