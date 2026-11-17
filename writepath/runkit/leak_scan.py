"""Before results leave the lab host: does any result or log file contain a secret value of this stream, in any
representation the stream constructs? Version 2 (review A, A4 to A6); design and limits: runkit/GATE-DESIGN.md section 3.

Inventory, read at scan time from the SOURCES and never printed:
  WP_HOME/secrets*/ and WP_HOME/oldhome/:
    pgpass*                the password field of every entry (pgpass escapes undone); user and host are not secret
    es_api_key*.json       the API key itself (decoded from `encoded`), and `encoded` whole; the key id is NOT secret
    *.crt / *.pem certs    public, skipped (listed)
    any other file         its stripped content (pg_password, pw_*, es_elastic_password, es_broker_password, ...);
                           an unknown JSON file: each string value except id/name/expiration/type
  key store (the KMS stand-in, also the req_hmac key): subject_keys.key_hex and tenant_keys.hkey_hex, read through
    `docker exec <PG container> psql` (conf pg_container, pg_user, keys_db) unless --no-keystore REASON is given.
Representations: the raw value, hex (both cases), base64 and base64url at all three byte alignments (so base64 of
`user:password` for ANY user, of an `id:key` pair, or of any document holding the value), and for key-store keys the
raw bytes too. Each scanned file is searched raw, with whitespace removed (values split or wrapped across lines), with
backslash escapes undone (JSON \\uXXXX, \\n, \\/, quotes), and percent-decoded; gzip files are also searched
decompressed. A file-held secret is found from ANY 16-character fragment of a representation; a key-store key from a
complete representation.
Every scan first runs a self-test: FAKE values (generated in memory) planted in every representation in a temporary
tree must all be found, and non-secret controls (key ids, user names, hosts) must not be. Output: file paths, counts,
representation classes and source names only; never a value or a fragment.
Exit 0: no hit. 1: a hit. 2: cannot scan (self-test failed, a source unreadable, no inventory, a scanned file
unreadable).
usage: python3 leak_scan.py WP_HOME DIR [DIR ...] [--keystore-container NAME] [--no-keystore REASON]
library: representations(value), find(data, values) for an issuing process's own check before it disposes of a key."""
import base64
import gzip
import hashlib
import json
import os
import pathlib
import re
import secrets as _rnd
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

VERSION = "2"
W = 16                         # fragment window: any 16 characters of a representation of a file-held secret
MIN_LEN = 8                    # shorter values are not inventoried (reported)
NOT_SECRET_JSON = {"id", "name", "expiration", "type"}
WS = b" \t\r\n\f\v"


class ScanError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- representations
def _b64_cores(v):
    """The base64 characters fully determined by v when v sits at byte offset 0, 1 or 2 (mod 3) of a longer
    base64-encoded stream: catches v inside base64(user:v), base64(id:v) or base64 of any document."""
    out = []
    for k in (0, 1, 2):
        e = base64.b64encode(b"\0" * k + v)
        core = e[-(-8 * k // 6):(8 * (k + len(v))) // 6]
        out += [("base64", core), ("base64url", core.translate(bytes.maketrans(b"+/", b"-_")))]
    return out


def representations(value):
    """[(class, bytes)] for one secret value (bytes or str)."""
    v = value.encode() if isinstance(value, str) else bytes(value)
    reps = [("raw", v), ("hex", v.hex().encode()), ("HEX", v.hex().upper().encode())] + _b64_cores(v)
    return [(c, r) for c, r in reps if r]


def _keystore_reps(hexkey):
    """A key-store key: its stored hex text, its raw bytes and their encodings."""
    raw = bytes.fromhex(hexkey)
    return [(c, r) for c, r in representations(raw) + [("b64-of-hex", x) for _, x in _b64_cores(hexkey.encode())]]


# ------------------------------------------------------------------------------------------------------- inventory
def _pgpass_entries(text):
    """(user, password) per pgpass line; ':' and '\\' are backslash-escaped in each field."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields, cur, esc = [], "", False
        for ch in line:
            if esc:
                cur, esc = cur + ch, False
            elif ch == "\\":
                esc = True
            elif ch == ":" and len(fields) < 4:
                fields.append(cur)
                cur = ""
            else:
                cur += ch
        fields.append(cur)
        if len(fields) == 5:
            out.append((fields[3], fields[4]))
    return out


def load_inventory(home, keystore=None, no_keystore=None):
    """{"file": [(source, value bytes)], "keystore": [(source, hex)], "controls": [bytes], "coverage": [lines]}."""
    home = pathlib.Path(home)
    dirs = [d for d in sorted(home.iterdir()) if d.is_dir() and d.name.startswith("secrets")] if home.is_dir() else []
    if (home / "oldhome").is_dir():
        dirs.append(home / "oldhome")
    inv = {"file": [], "keystore": [], "controls": [], "coverage": [], "sources": [str(d.resolve()) for d in dirs]}
    if not dirs:
        raise ScanError(f"no secrets directory under {home}")
    for d in dirs:
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                p = pathlib.Path(root) / fn
                try:
                    raw = p.read_bytes()
                except OSError as e:
                    raise ScanError(f"cannot read the secret source {p} ({type(e).__name__})") from None
                rel = str(p.relative_to(home))
                vals = []
                if fn.startswith("pgpass") or (d.name == "oldhome" and fn == "pgpass"):
                    for user, pw in _pgpass_entries(raw.decode("utf-8", "replace")):
                        vals.append((pw, "frag"))
                        inv["controls"].append(user.encode())
                elif fn.endswith((".crt", ".pem")) and b"BEGIN CERTIFICATE" in raw and b"PRIVATE KEY" not in raw:
                    inv["coverage"].append(f"skipped {rel}: public certificate")
                    continue
                elif fn.startswith("es_api_key") and fn.endswith(".json"):
                    j = json.loads(raw)
                    kid, enc = j["id"], j["encoded"]
                    key = base64.b64decode(enc).decode().split(":", 1)[1]
                    vals += [(key, "frag"), (enc, "full")]   # enc's head encodes the public id: full match only
                    inv["controls"].append(kid.encode())
                else:
                    try:
                        j = json.loads(raw)
                    except ValueError:
                        j = None
                    if isinstance(j, (dict, list)):
                        stack = [j]
                        while stack:
                            o = stack.pop()
                            if isinstance(o, dict):
                                for k, x in o.items():
                                    if k in NOT_SECRET_JSON and isinstance(x, str):
                                        inv["controls"].append(x.encode())
                                    else:
                                        stack.append(x)
                            elif isinstance(o, list):
                                stack.extend(o)
                            elif isinstance(o, str):
                                vals.append((o, "frag"))
                    else:
                        vals.append((raw.decode("utf-8", "replace").strip(), "frag"))
                kept = [(v, m) for v, m in vals if len(v) >= MIN_LEN]
                inv["file"] += [(rel, v.encode(), m) for v, m in kept]
                inv["coverage"].append(f"source {rel}: {len(kept)} value(s)" +
                                       (f", {len(vals) - len(kept)} under {MIN_LEN} chars not inventoried" if len(vals) > len(kept) else ""))
    if no_keystore:
        inv["coverage"].append(f"key store: NOT COVERED ({no_keystore})")
    else:
        conf = lambda n, d=None: ((home / "conf" / n).read_text().strip() if (home / "conf" / n).exists() else d)  # noqa: E731
        c = keystore or conf("pg_container")
        if not c:
            raise ScanError("key store: no PG container named (conf/pg_container or --keystore-container)")
        q = "select key_hex from subject_keys union all select hkey_hex from tenant_keys"
        try:
            r = subprocess.run(["docker", "exec", c, "psql", "-U", conf("pg_user", "wp"), "-d", conf("keys_db", "wp_keys"),
                                "-At", "-v", "ON_ERROR_STOP=1", "-c", q], capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ScanError(f"key store not readable ({type(e).__name__}); pass --no-keystore REASON to scan without it") from None
        if r.returncode != 0:
            raise ScanError(f"key store not readable (psql exit {r.returncode}); pass --no-keystore REASON to scan without it")
        keys = [x for x in r.stdout.decode().split() if re.fullmatch(r"[0-9a-fA-F]{32,}", x) and len(x) % 2 == 0]
        inv["keystore"] = [("keystore", k.lower()) for k in keys]
        inv["coverage"].append(f"key store: {len(keys)} live key(s); keys destroyed before this scan cannot be checked")
    if not inv["file"] and not inv["keystore"]:
        raise ScanError("no secret value inventoried: nothing to compare against")
    inv["coverage"].append("NOT COVERED: broker-minted and probe API keys (memory only; the issuing process must check "
                           "with representations()); SCRAM verifiers (salted); values rotated or destroyed before this scan")
    return inv


# ------------------------------------------------------------------------------------------------------- scanning
class Index:
    def __init__(self, inv):
        self.win, self.short = {}, []
        for src, v, mode in inv["file"]:
            for cls, r in representations(v):
                if len(r) < W:
                    self.short.append((r, cls, src))
                elif mode == "full":
                    self.win.setdefault(r[:W], []).append((cls, src, r))
                else:
                    for i in range(len(r) - W + 1):
                        self.win.setdefault(r[i:i + W], []).append((cls, src, None))
        for src, hx in inv["keystore"]:
            for cls, r in _keystore_reps(hx):
                if len(r) >= W:
                    self.win.setdefault(r[:W], []).append((cls, src, r))

    def find(self, data):
        hits = set()
        for r, cls, src in self.short:
            if r in data:
                hits.add((cls, src))
        win = self.win
        for i in range(len(data) - W + 1):
            e = win.get(data[i:i + W])
            if e:
                for cls, src, full in e:
                    if full is None or data.startswith(full, i):
                        hits.add((cls, src))
        return hits


_ESC = re.compile(rb"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)


def _unescape(b):
    def sub(m):
        t = m.group(1)
        if t[:1] == b"u":
            return chr(int(t[1:], 16)).encode("utf-8", "replace")
        if t[:1] == b"x" and len(t) == 3:
            return bytes([int(t[1:], 16)])
        return {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"f": b"\f", b"b": b"", b"v": b"\v"}.get(t, t)
    return _ESC.sub(sub, b)


def views(data):
    out = [data]
    v1 = data.translate(None, WS)
    v2 = _unescape(data).translate(None, WS) if b"\\" in data else v1
    v3 = urllib.parse.unquote_to_bytes(v2).translate(None, WS) if b"%" in v2 else v2
    for v in (v1, v2, v3):
        if v not in out:
            out.append(v)
    if data[:2] == b"\x1f\x8b":
        try:
            out += views(gzip.decompress(data)[:512 * 1024 * 1024])
        except (OSError, EOFError):
            pass
    return out


def find(data, values):
    """For an issuing process: the representation classes of `values` (secrets in memory) found in `data`."""
    idx = Index({"file": [("in-process", v.encode() if isinstance(v, str) else v, "frag") for v in values], "keystore": []})
    return sorted({c for v in views(data) for c, _ in idx.find(v)})


def scan(dirs, idx, exclude=()):
    hits, scanned, unreadable = {}, 0, []
    ex = [os.path.realpath(e) for e in exclude]
    for d in dirs:
        if any(os.path.realpath(d).startswith(x) for x in ex):
            continue
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if not any(os.path.realpath(os.path.join(root, s)).startswith(x) for x in ex)]
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    data = open(p, "rb").read()
                except OSError:
                    unreadable.append(p)
                    continue
                scanned += 1
                h = set()
                for v in views(data):
                    h |= idx.find(v)
                if h:
                    hits[p] = h
    return hits, scanned, unreadable


# ------------------------------------------------------------------------------------------------------ self-test
def self_test():
    """Plant FAKE values in every representation; all must be found and no control may be. Returns (found, planted,
    control hits). No value is printed."""
    pw = _rnd.token_urlsafe(32)                                     # like the PG and broker passwords
    espw = "".join(_rnd.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789~!@#$%^&*-_=+?") for _ in range(20))
    kid, key = _rnd.token_urlsafe(15)[:20], _rnd.token_urlsafe(16)[:22]
    enc = base64.b64encode(f"{kid}:{key}".encode()).decode()
    kms = _rnd.token_bytes(32)
    b64 = lambda s: base64.b64encode(s.encode() if isinstance(s, str) else s).decode()  # noqa: E731
    long_doc = b64(b"x" * 49 + pw.encode() + b"y" * 50)
    planted = {
        "json_field": json.dumps({"note": pw}), "pgpass_line": f"127.0.0.1:5432:*:wp_app:{pw}",
        "url": f"postgresql://wp:{pw}@127.0.0.1:5432/wp_events", "url_percent": "http://elastic:" +
        urllib.parse.quote(espw, safe="") + "@127.0.0.1:9200/", "hex": pw.encode().hex(), "hex_upper": pw.encode().hex().upper(),
        "base64": b64(pw), "base64url": base64.urlsafe_b64encode(pw.encode()).decode(),
        "basic_elastic": "Authorization: Basic " + b64(f"elastic:{espw}"),
        "basic_broker": "Authorization: Basic " + b64(f"wp16096_broker:{pw}"),
        "apikey_header": "Authorization: ApiKey " + enc, "apikey_idkey": f"{kid}:{key}", "apikey_key": f'{{"k": "{key}"}}',
        "json_unicode_escape": '{"k": "\\u%04x%s"}' % (ord(key[0]), key[1:]),
        "split_lines": pw[:10] + "\n   " + pw[10:], "split_json_escape": '"' + pw[:10] + "\\n" + pw[10:] + '"',
        "base64_wrapped": "\n".join(long_doc[i:i + 76] for i in range(0, len(long_doc), 76)),
        "shell_quoted": f"export X='{espw}'", "fragment": "...." + pw[5:27] + "....",
        "kms_hex": kms.hex(), "kms_hex_upper": kms.hex().upper(), "kms_base64": b64(kms), "kms_raw": kms,
        "gzip": gzip.compress(f"line\n{pw}\n".encode()),
    }
    controls = f"{kid}\nwp_app\nwp16096_broker\nelastic\n127.0.0.1:5432\n{b64(kid)}\n{_rnd.token_urlsafe(32)}\n"
    tmp = tempfile.mkdtemp()
    try:
        home = pathlib.Path(tmp) / "home"
        (home / "secrets").mkdir(parents=True)
        (home / "secrets" / "pgpass.wp_app").write_text(f"127.0.0.1:5432:*:wp_app:{pw}\n")
        (home / "secrets" / "es_elastic_password").write_text(espw + "\n")
        (home / "secrets" / "es_api_key.verifier.json").write_text(json.dumps({"id": kid, "encoded": enc}))
        res = pathlib.Path(tmp) / "res"
        res.mkdir()
        for n, v in planted.items():
            (res / n).write_bytes(v if isinstance(v, bytes) else v.encode())
        (res / "controls").write_text(controls)
        inv = load_inventory(home, no_keystore="self-test")
        inv["keystore"] = [("keystore", kms.hex())]
        hits = scan([str(res)], Index(inv))[0]
        found = sum(1 for n in planted if str(res / n) in hits)
        return found, len(planted), int(str(res / "controls") in hits)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    ks = nks = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--keystore-container", "--no-keystore") and i + 1 < len(argv):
            ks, nks = (argv[i + 1], nks) if argv[i] == "--keystore-container" else (ks, argv[i + 1])
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    if len(rest) < 2:
        print(__doc__)
        return 2
    home, dirs = rest[0], rest[1:]
    me = pathlib.Path(__file__).read_bytes()
    print(f"leak_scan version {VERSION} sha256 {hashlib.sha256(me).hexdigest()}")
    found, planted, ctl = self_test()
    print(f"self-test: {found}/{planted} planted representations found, {ctl} control hit(s)")
    if found != planted or ctl:
        print("leak_scan: SELF-TEST FAILED, no scan made")
        return 2
    try:
        inv = load_inventory(home, ks, nks)
    except (ScanError, OSError, ValueError, KeyError, IndexError) as e:
        print(f"leak_scan: CANNOT SCAN: {e if isinstance(e, ScanError) else type(e).__name__}")
        return 2
    for c in inv["coverage"]:
        print(c)
    print(f"candidate values: {len(inv['file'])} from files, {len(inv['keystore'])} from the key store")
    hits, scanned, unreadable = scan(dirs, Index(inv), exclude=inv["sources"])
    print(f"files scanned: {scanned} unreadable: {len(unreadable)} files with a secret value: {len(hits)}")
    for p in unreadable:
        print("UNREADABLE", p)
    for p, h in sorted(hits.items()):
        print("HIT", p, len(h), "classes=" + ",".join(sorted({c for c, _ in h})), "sources=" + ",".join(sorted({s for _, s in h})))
    return 1 if hits else (2 if unreadable or not scanned else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
