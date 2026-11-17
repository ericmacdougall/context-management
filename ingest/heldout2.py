"""Fresh held-out set for the scanner (PREREG-amend-4 A3). The rule below was committed BEFORE scan.py was changed and before this
module was run against any scanner; see the amendment for the order. Strings that look like credential formats are assembled at run
time from fragments so this file carries no credential-shaped literal. All values are synthetic.

item12c is the only place this set is scored. It scores the tuned set, the burned held-out set of add7c86 and the fresh set once."""
from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import urllib.parse
from typing import Callable, Dict, List, Tuple

from . import heldout as H
from .canon import Hasher
from .proofs_common import write_artifact
from .scan import ReferenceScanner, redact_text

BASE_COMMIT = "add7c86881ae2793ec32ecc3fa0f79dfd8d8add4"
SEED = int(hashlib.sha256(("fresh-heldout|" + BASE_COMMIT).encode()).hexdigest()[:8], 16)
ALNUM = string.ascii_letters + string.digits
B64 = string.ascii_letters + string.digits + "+/"
FAMILIES = ["sk", "aws", "ghp", "bearer", "pwkv", "pem"]
TRANSFORMS = ["identity", "base64", "base64_wrap24", "hex_lower", "dot_split", "tab_split", "percent", "base32_lower"]
N_PER_CELL = 40
N_PER_BENIGN = 40

_PEM_HEAD = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PEM_FOOT = "-----END " + "RSA PRIVATE KEY-----"


def cells() -> List[Tuple[str, str]]:
    return random.Random(SEED).sample([(f, t) for f in FAMILIES for t in TRANSFORMS], 24)


def _rs(r, alphabet, n): return "".join(r.choice(alphabet) for _ in range(n))


def credential(family: str, r) -> Tuple[str, List[str]]:
    """(credential string, random parts that are credential material)."""
    if family == "sk":
        body = _rs(r, ALNUM, 24)
        return "sk-" + "live-" + body, [body]
    if family == "aws":
        body = _rs(r, string.ascii_uppercase + string.digits, 16)
        return "AK" + "IA" + body, [body]
    if family == "ghp":
        body = _rs(r, ALNUM, 36)
        return "gh" + "p_" + body, [body]
    if family == "bearer":
        tok = _rs(r, ALNUM, 40)
        return "Authorization: Bearer " + tok, [tok]
    if family == "pwkv":
        v = _rs(r, ALNUM, 14)
        return "pass" + "word=" + v, [v]
    lines = [_rs(r, B64, 64) for _ in range(3)]
    return "\n".join([_PEM_HEAD] + lines + [_PEM_FOOT]), lines + [_PEM_HEAD]


def _wrap(s: str, n: int) -> str: return "\n".join(s[i:i + n] for i in range(0, len(s), n))


TRANSFORM_FN: Dict[str, Callable[[str], str]] = {
    "identity": lambda s: s,
    "base64": lambda s: base64.b64encode(s.encode()).decode(),
    "base64_wrap24": lambda s: _wrap(base64.b64encode(s.encode()).decode(), 24),
    "hex_lower": lambda s: s.encode().hex(),
    "dot_split": lambda s: ".".join(s),
    "tab_split": lambda s: "\t".join(s),
    "percent": lambda s: "".join("%%%02x" % c for c in s.encode()),
    "base32_lower": lambda s: base64.b32encode(s.encode()).decode().lower(),
}


def example(family: str, transform: str, i: int, seed: int = SEED) -> dict:
    r = random.Random("%d|%s|%s|%d" % (seed, family, transform, i))
    cred, parts = credential(family, r)
    v = TRANSFORM_FN[transform](cred)
    sens = list(parts) + ([] if transform == "identity" else [v])
    return {"text": "note: %s end" % v, "v": v, "sensitive": sens, "secret": cred}


# ----------------------------------------------------------------------------- benign
_WORDS = H._WORDS


def _pemlike(kind):
    def gen(r):
        lines = [_rs(r, B64, 64) for _ in range(3)]
        return "\n".join(["-----BEGIN " + kind + "-----"] + lines + ["-----END " + kind + "-----"])
    return gen


BENIGN2: Dict[str, Callable] = dict(H.BENIGN)
BENIGN2.update({
    "certificate_block": _pemlike("CERTIFICATE"),
    "public_key_block": _pemlike("PUBLIC KEY"),
    "known_hosts_line": lambda r: "host%d.example.org ssh-ed25519 AAAA%s" % (r.randint(1, 99), _rs(r, B64, 40)),
    "data_uri_random_bytes": lambda r: "data:image/png;base64," + base64.b64encode(bytes(r.getrandbits(8) for _ in range(90))).decode(),
    "changelog_line": lambda r: "- %s: %s %s (#%d)" % (r.choice(["fix", "feat", "docs"]), r.choice(_WORDS), r.choice(_WORDS), r.randint(1, 9999)),
    "sql_statement": lambda r: "SELECT %s, %s FROM %s WHERE id = %d" % (H._lower(r, 4, 8), H._lower(r, 4, 8), H._lower(r, 5, 10), r.randint(1, 10 ** 6)),
})


def _score_rows(scanner, hasher, items):
    rows = []
    for name, i, ex in items:
        out = redact_text(ex["text"], scanner, hasher)
        sc = H.score_example(ex["text"], ex["v"], ex["secret"], out, ex["sensitive"])
        rows.append({"cell": name, "i": i, **sc})
    return rows


def _agg(rows: List[dict]) -> dict:
    n = len(rows)
    out = {"n": n}
    for k in ("detected", "whole_string_absent", "complete_redaction"):
        c = sum(r[k] for r in rows)
        out[k] = c
        out[k + "_rate"] = c / n if n else 0.0
        out[k + "_wilson95_lower"] = H.wilson_lower(c, n)
    return out


def item12c(out: str, seed: int = SEED, directory_tag: str = "item12c") -> dict:
    sc, hs = ReferenceScanner(), Hasher(b"heldout-test-key")
    items = [("%s/%s" % (f, t), i, example(f, t, i, seed)) for f, t in cells() for i in range(N_PER_CELL)]
    fresh_rows = _score_rows(sc, hs, items)
    tuned = H._catch(H.TUNED, 50, 3)
    burned = H._catch(H.HELDOUT, 50, 9)
    ben_rows = []
    for name, fn in BENIGN2.items():
        for i in range(N_PER_BENIGN):
            r = random.Random("%d|%s|%d" % (seed, name, i))
            text = "note: %s ok" % fn(r)
            ben_rows.append({"class": name, "i": i, "changed": redact_text(text, sc, hs) != text})
    per_cell = {}
    for r in fresh_rows:
        q = per_cell.setdefault(r["cell"], [])
        q.append(r)
    S = {"scan_py_sha1": hashlib.sha1(open(__import__("ingest.scan", fromlist=["x"]).__file__, "rb").read()).hexdigest(),
         "seed": seed, "cells": len(per_cell), "label_fresh": "fresh held-out (rule committed before scan.py changed); synthetic, positive-only, equally weighted; not precision, not deployment recall",
         "fresh": _agg(fresh_rows), "fresh_per_cell": {k: _agg(v) for k, v in per_cell.items()},
         "tuned_set_for_comparison": {k: v for k, v in tuned.items() if k not in ("rows", "per_variant")},
         "burned_heldout_for_comparison": {k: v for k, v in burned.items() if k not in ("rows", "per_variant")},
         "burned_heldout_per_class": {k: p["complete_redaction"] for k, p in burned["per_variant"].items()},
         "tuned_per_class": {k: p["complete_redaction"] for k, p in tuned["per_variant"].items()},
         "scanner_at_add7c86": {"tuned_complete_redaction": "400/400", "burned_heldout_complete_redaction": "300/800", "burned_heldout_whole_string_absent": "350/800"},
         "benign_fresh": {"n": len(ben_rows), "changed": sum(r["changed"] for r in ben_rows),
                          "per_class_changed": {n: sum(r["changed"] for r in ben_rows if r["class"] == n) for n in BENIGN2}}}
    S["benign_fresh"]["false_positive_rate"] = S["benign_fresh"]["changed"] / len(ben_rows)
    for k in ("tuned_set_for_comparison", "burned_heldout_for_comparison"):
        S[k]["wilson95_lower_complete_redaction"] = S[k]["overall_wilson95_lower_complete_redaction"]
    runs = {"fresh_rows": fresh_rows, "tuned_rows": tuned["rows"], "burned_heldout_rows": burned["rows"], "benign_rows": ben_rows}
    S["artifact"] = write_artifact(out, directory_tag, {"families": FAMILIES, "transforms": TRANSFORMS, "cells": ["%s/%s" % c for c in cells()],
                                                         "n_per_cell": N_PER_CELL, "K": H.K_FRAGMENT}, runs, S)
    return S
