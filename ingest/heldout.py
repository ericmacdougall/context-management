"""Held-out scanner variants (PREREG-amend-2 A9). Written AFTER ingest/scan.py was frozen (blob b4b63d74fe7d, commit aa30130) by a
generator that was not used while tuning. The scanner is NOT edited after seeing these results; if it is, this set is burned.
The tuned set is the 8 variants of item 5 measured the same way for comparison."""
from __future__ import annotations

import base64
import binascii
import json
import math
import random
import string
import urllib.parse
from typing import Callable, Dict, List

from .canon import Hasher
from .proofs_common import write_artifact
from .scan import ReferenceScanner, redact_text

ALNUM = string.ascii_letters + string.digits


def _sk(r): return "sk-live-" + "".join(r.choice(ALNUM) for _ in range(24))
def _tok(r): return "".join(r.choice(ALNUM) for _ in range(32))


HELDOUT: Dict[str, Callable] = {
    "base32": lambda s, r: base64.b32encode(s.encode()).decode(),
    "hex_upper": lambda s, r: s.encode().hex().upper(),
    "base64_wrapped_20": lambda s, r: "\n".join(base64.b64encode(s.encode()).decode()[i:i + 20] for i in range(0, 60, 20)) + base64.b64encode(s.encode()).decode()[60:],
    "quoted_printable": lambda s, r: "".join("=%02X" % c for c in s.encode()),
    "html_numeric_entities": lambda s, r: "".join("&#%d;" % ord(c) for c in s),
    "octal_escapes": lambda s, r: "".join("\\%03o" % ord(c) for c in s),
    "reversed": lambda s, r: s[::-1],
    "hyphen_split": lambda s, r: "-".join(s),
    "space_split": lambda s, r: " ".join(s),
    "base64_of_hex": lambda s, r: base64.b64encode(s.encode().hex().encode()).decode(),
    "double_urlencoded": lambda s, r: urllib.parse.quote("".join("%%%02x" % c for c in s.encode()), safe=""),
    "underscore_prefix_sk": lambda s, r: s.replace("sk-live-", "sk_live_"),
    "bearer_header": lambda s, r: "Authorization: Bearer " + _tok(r),
    "aws_access_key": lambda s, r: "AKIA" + "".join(r.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
    "password_kv": lambda s, r: "password=" + _tok(r)[:12],
    "private_key_header": lambda s, r: "-----BEGIN RSA PRIVATE KEY-----\nMIIE" + _tok(r),
}
TUNED = {
    "plain": lambda s, r: s, "base64": lambda s, r: base64.b64encode(s.encode()).decode(), "hex": lambda s, r: s.encode().hex(),
    "urlencoded": lambda s, r: "".join("%%%02x" % c for c in s.encode()), "double_base64": lambda s, r: base64.b64encode(base64.b64encode(s.encode())).decode(),
    "json_unicode": lambda s, r: "".join("\\u%04x" % c for c in s.encode()), "zero_width_split": lambda s, r: "​".join(s),
    "base64url": lambda s, r: base64.urlsafe_b64encode(s.encode()).decode().rstrip("="),
}


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


def _catch(variants: Dict[str, Callable], n: int, seed0: int) -> dict:
    sc, h = ReferenceScanner(), Hasher(b"heldout-test-key")
    per, rows = {}, []
    for name, fn in variants.items():
        caught = 0
        for i in range(n):
            r = random.Random(seed0 * 1000 + i)
            secret = _sk(r)
            v = fn(secret, r)
            text = "my credential is %s thanks" % v
            out = redact_text(text, sc, h)
            ok = (v not in out) and (secret not in out)
            caught += ok
            rows.append({"variant": name, "i": i, "caught": ok})
        per[name] = {"n": n, "caught": caught, "rate": caught / n}
    tot_n = sum(p["n"] for p in per.values())
    tot_k = sum(p["caught"] for p in per.values())
    return {"per_variant": per, "overall_n": tot_n, "overall_caught": tot_k, "overall_rate": tot_k / tot_n, "overall_wilson95_lower": wilson_lower(tot_k, tot_n)}


def item12(out: str) -> dict:
    held = _catch(HELDOUT, 50, 9)
    tuned = _catch(TUNED, 50, 3)
    S = {"scan_py_blob": "b4b63d74fe7dd33c31416802e9bae7fe05e57697", "heldout": held, "tuned_for_comparison": tuned}
    S["artifact"] = write_artifact(out, "item12", {"n_per_variant": 50, "heldout_variants": list(HELDOUT), "tuned_variants": list(TUNED)}, {"heldout": held["per_variant"], "tuned": tuned["per_variant"]}, S)
    return S
