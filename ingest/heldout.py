"""Scanner variants used before the scanner repair of PREREG-amend-4 (BURNED).
HELDOUT was registered as a held-out set against the scanner frozen at aa30130 (blob b4b63d74fe7d). The repository records the order of
commits but cannot show that the generator was independent of earlier tuning; that is asserted by the previous owner, not demonstrated.
After add7c86 the scanner was repaired using these variants, so HELDOUT and TUNED are both BURNED for the held-out role: they remain only to
compare old and new scanner figures. The fresh held-out set is ingest/heldout2.py (rule committed before the repair)."""
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


K_FRAGMENT = 8          # PREREG-amend-3 A2: a surviving contiguous fragment of this many characters of any sensitive string is a leak


def _default_sensitive(secret: str, v: str) -> List[str]:
    return [v, secret]


# Sensitive strings per held-out class (PREREG-amend-3 A2). Classes not listed encode the generated sk-live secret: v and the secret.
SENSITIVE: Dict[str, Callable] = {
    "bearer_header": lambda secret, v: [v.split("Bearer ", 1)[1]],
    "aws_access_key": lambda secret, v: [v],
    "password_kv": lambda secret, v: [v.split("password=", 1)[1]],
    "private_key_header": lambda secret, v: ["-----BEGIN RSA PRIVATE KEY-----", v.split("\n", 1)[1]],
}


def survives(s: str, out: str, k: int = K_FRAGMENT) -> bool:
    """True when some contiguous k-character fragment of s (the whole string if shorter) occurs in out."""
    if len(s) <= k:
        return s in out
    return any(s[i:i + k] in out for i in range(len(s) - k + 1))


def longest_surviving(strings: List[str], out: str) -> int:
    import difflib
    return max((difflib.SequenceMatcher(None, s, out, autojunk=False).find_longest_match(0, len(s), 0, len(out)).size for s in strings), default=0)


def score_example(text: str, v: str, secret: str, out: str, sensitive: List[str]) -> dict:
    """Old predicate (whole_string_absent, as scored at 0801866) next to the corrected one (complete_redaction). Integers and booleans only:
    no surviving credential-shaped string is returned."""
    return {"whole_string_absent": (v not in out) and (secret not in out),
            "complete_redaction": not any(survives(x, out) for x in sensitive),
            "detected": out != text,
            "longest_surviving_fragment": longest_surviving(sensitive, out)}


def _catch(variants: Dict[str, Callable], n: int, seed0: int, sens: Dict[str, Callable] = SENSITIVE) -> dict:
    sc, h = ReferenceScanner(), Hasher(b"heldout-test-key")
    per, rows = {}, []
    for name, fn in variants.items():
        acc = {"whole_string_absent": 0, "complete_redaction": 0, "detected": 0}
        for i in range(n):
            r = random.Random(seed0 * 1000 + i)
            secret = _sk(r)
            v = fn(secret, r)
            text = "my credential is %s thanks" % v
            out = redact_text(text, sc, h)
            sc_row = score_example(text, v, secret, out, sens.get(name, _default_sensitive)(secret, v))
            for k in acc:
                acc[k] += sc_row[k]
            rows.append({"variant": name, "i": i, **sc_row})
        per[name] = {"n": n, **acc, "rate_whole_string_absent": acc["whole_string_absent"] / n, "rate_complete_redaction": acc["complete_redaction"] / n}
    tot_n = sum(p["n"] for p in per.values())
    res = {"per_variant": per, "overall_n": tot_n, "rows": rows}
    for k in ("whole_string_absent", "complete_redaction", "detected"):
        tk = sum(p[k] for p in per.values())
        res["overall_" + k] = tk
        res["overall_rate_" + k] = tk / tot_n
        res["overall_wilson95_lower_" + k] = wilson_lower(tk, tot_n)
    return res


def item12(out: str) -> dict:
    """Re-scoring of the SAME generators and seeds as 0801866 (scan.py frozen, blob b4b63d74). Both predicates reported."""
    held = _catch(HELDOUT, 50, 9)
    tuned = _catch(TUNED, 50, 3)
    def strip(x): return {k: v for k, v in x.items() if k != "rows"}
    changed = sorted(k for k, p in held["per_variant"].items() if p["whole_string_absent"] != p["complete_redaction"])
    S = {"scan_py_blob": "b4b63d74fe7dd33c31416802e9bae7fe05e57697", "K_fragment": K_FRAGMENT, "heldout": strip(held), "tuned_for_comparison": strip(tuned),
         "heldout_classes_whose_score_changes_under_complete_redaction": changed,
         "label": "re-scored, same set and seeds as 0801866; positive-only, equally weighted synthetic examples; not precision, not a false-positive rate, not deployment recall"}
    S["artifact"] = write_artifact(out, "item12", {"n_per_variant": 50, "heldout_variants": list(HELDOUT), "tuned_variants": list(TUNED), "K": K_FRAGMENT},
                                   {"heldout_rows": held["rows"], "tuned_rows": tuned["rows"]}, S)
    return S


# ------------------------------------------------------------------------------ benign corpus (PREREG-amend-3 A2, rule fixed before the scan)
_WORDS = ("table river window garden pencil morning coffee letter bridge lantern silver forest ticket harbor violin planet cabinet "
          "orange thunder pocket meadow compass blanket ladder whistle canvas marble anchor basket feather dinner engine mirror "
          "pillow rocket saddle tunnel village yellow").split()


def _english(r): return " ".join(r.choice(_WORDS) for _ in range(8))
def _lower(r, a, b): return "".join(r.choice(string.ascii_lowercase) for _ in range(r.randint(a, b)))


BENIGN: Dict[str, Callable] = {
    "english": lambda r: _english(r),
    "uuid4": lambda r: str(__import__("uuid").UUID(int=r.getrandbits(128), version=4)),
    "git_sha1_hex40": lambda r: "%040x" % r.getrandbits(160),
    "base64_of_english": lambda r: base64.b64encode(_english(r).encode()).decode(),
    "hex_of_english": lambda r: _english(r).encode().hex(),
    "percent_encoded_english": lambda r: urllib.parse.quote(_english(r)),
    "long_file_path": lambda r: "/usr/local/lib/python3.9/site-packages/%s/__init__.py" % _lower(r, 12, 20),
    "camel_identifier": lambda r: "".join(r.choice(_WORDS).capitalize() for _ in range(4))[:24],
    "url_long_slug": lambda r: "https://example.org/blog/" + "-".join(r.choice(_WORDS) for _ in range(6))[:40],
    "numeric_log_line": lambda r: "2026-11-%02dT%02d:%02d:%02dZ pid=%d bytes=%d code=%d" % (r.randint(1, 28), r.randint(0, 23), r.randint(0, 59), r.randint(0, 59), r.randint(100, 99999), r.randint(1, 10 ** 7), r.choice([200, 301, 404, 500])),
    "json_snippet": lambda r: json.dumps({"id": r.randint(1, 10 ** 6), "name": _lower(r, 5, 12), "tags": [_lower(r, 3, 6), _lower(r, 3, 6)]}),
    "sha256_hex64": lambda r: "%064x" % r.getrandbits(256),
}


def item12b(out: str) -> dict:
    sc, h = ReferenceScanner(), Hasher(b"heldout-test-key")
    per, rows = {}, []
    for name, fn in BENIGN.items():
        ch = 0
        for i in range(50):
            r = random.Random(31 + i)
            x = fn(r)
            text = "note: %s ok" % x
            changed = redact_text(text, sc, h) != text
            ch += changed
            rows.append({"class": name, "i": i, "changed": changed})
        per[name] = {"n": 50, "changed": ch, "rate": ch / 50}
    n = sum(p["n"] for p in per.values())
    k = sum(p["changed"] for p in per.values())
    S = {"scan_py_blob": "b4b63d74fe7dd33c31416802e9bae7fe05e57697", "per_class": per, "overall_n": n, "overall_changed": k, "overall_false_positive_rate": k / n,
         "label": "false-positive rate on THIS synthetic benign set only; not a deployment rate"}
    S["artifact"] = write_artifact(out, "item12b", {"classes": list(BENIGN), "n_per_class": 50, "seed_rule": "random.Random(31+i) per example i"}, rows, S)
    return S
