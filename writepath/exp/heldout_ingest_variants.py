"""H2 of THRESHOLDS amendment 15: the ingest stream's held-out transforms, vendored VERBATIM (functions, generator,
seed and text frame) from ingest/heldout.py on feat/port-ingest (blob 3e4f3dc057d2f32480b1a8a73dd58b3f6243315f,
commit a7f0529, 2026-11-16 12:25 -0700). That file was written by the ingest owner for its own scanner, after this
stream's scanner (wp/secrets.py) last changed; nothing here was chosen with wp/secrets.py in view. Every value is
synthetic (random characters at runtime). The ingest judge was redaction; this stream's judge is scan() (amendment 15).
"""
import base64
import random
import string
import urllib.parse

ALNUM = string.ascii_letters + string.digits


def _sk(r): return "sk-live-" + "".join(r.choice(ALNUM) for _ in range(24))
def _tok(r): return "".join(r.choice(ALNUM) for _ in range(32))


HELDOUT = {
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
N_PER_VARIANT, SEED0 = 50, 9        # as ingest item12: _catch(HELDOUT, 50, 9)


def positives():
    """[{family, encoding, context, text, secret, value}] in the ingest generator's order and seeding."""
    out = []
    for name, fn in HELDOUT.items():
        for i in range(N_PER_VARIANT):
            r = random.Random(SEED0 * 1000 + i)
            secret = _sk(r)
            v = fn(secret, r)
            out.append({"family": "ingest_heldout", "encoding": name, "context": "ingest_frame", "index": i,
                        "text": "my credential is %s thanks" % v, "secret": secret, "value": v})
    return out
