"""Before results leave the lab host: does any result or log file, or any file or directory NAME, contain a secret
value of this stream, in any representation the stream constructs? Version 3 (review D F4, F5, F6, F12); design and
limits: runkit/GATE-DESIGN.md section 5.7 (section 3 for version 2).

Inventory, read at scan time and never printed. SITES below lists every place the stream creates or derives a secret
and how each is inventoried (tests/test_leak_scan.py fails on a creation site the table does not list):
  WP_HOME/secrets*/, WP_SECRETS_DIR when set, and WP_HOME/oldhome/:
    pgpass*                the password field of every entry (pgpass escapes undone); user and host are not secret
    es_api_key*.json       the API key itself (decoded from `encoded`), and `encoded` whole; the key id is NOT secret
    *.crt / *.pem certs    public, skipped (listed)
    any other file         its stripped content; an unknown JSON file: each string value except id/name/expiration/type
  key store (the KMS stand-in): subject_keys.key_hex and tenant_keys.hkey_hex, read through `docker exec <PG container>
    psql` (conf pg_container, pg_user, keys_db) unless --no-keystore REASON is given; and, derived from each tenant key,
    the tenant chain key (HMAC-SHA256 over "chain-key:v1", wp/keys.py chain_key; the key service sends it as hex).
Representations: the raw value, hex (both cases), base64 and base64url at all three byte alignments, UTF-16LE and
UTF-16BE of a non-ASCII value; for key-store and derived keys also the raw bytes and base64 of the hex.
Normalised views of each file: raw; NUL bytes removed (UTF-16 and UTF-32 text of ASCII values, at any alignment); with
whitespace removed; then up to three rounds of undoing backslash escapes and percent-encoding, until nothing changes.
Content starting with the gzip magic is decompressed (up to three levels) and viewed the same way.
Split rule, exactly: a file-held secret is found when 16 consecutive characters of one of its representations appear
in one normalised view; a key-store or derived key when a complete representation does. A value cut into pieces
shorter than 16 characters, encrypted, compressed other than by gzip (or gzip inside another encoding), or encoded in a
way not listed here is NOT found; nor is a value outside the inventory (SITES says which: keys held only in memory,
SCRAM internals; and values rotated or destroyed before the scan).
Fail closed: every requested root must exist; every directory the walk cannot list, file it cannot read, non-regular
file, dangling link, link to a directory outside the requested roots (not followed), damaged gzip stream and content
over the size limit (512 MiB per file and per decompressed stream; never searched in part silently) is listed and makes
the exit code 2. Nothing is excluded: a secret source inside a scanned root is a hit.
Output: counts, representation classes, and paths, source names and waiver reasons that were first checked against the
inventory; one that holds a candidate is printed as <redacted path #n> (or text). Never a value or a fragment.
Exit 0: no hit and nothing left unscanned. 1: a hit. 2: cannot scan, or something was not scanned.
usage: python3 leak_scan.py WP_HOME DIR [DIR ...] [--keystore-container NAME] [--no-keystore REASON]
library: representations(value); find(data, values) for an issuing process's own check before it disposes of a key
(raises ScanError for content it cannot search completely)."""
import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import secrets as _rnd
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import zlib

VERSION = "3"
W = 16                         # fragment window: any 16 characters of a representation of a file-held secret
MIN_LEN = 8                    # shorter values are not inventoried (reported)
NOT_SECRET_JSON = {"id", "name", "expiration", "type"}
WS = b" \t\r\n\f\v"
MAX_BYTES = int(os.environ.get("LEAK_SCAN_MAX_BYTES") or 512 * 1024 ** 2)   # lowered by the tests only
GZ_DEPTH = 3
ROUNDS = 3
CHAIN_LABEL = b"chain-key:v1"
LIMITS = ("LIMITS: a value is found when 16 consecutive characters of one listed representation appear in one "
          "normalised view (a key-store or derived key: a complete representation); pieces shorter than 16 "
          "characters, encrypted content, compression other than gzip, unlisted encodings and values outside the "
          "inventory (memory-only keys, SCRAM internals, rotated or destroyed values) are not found")

# Every place wp/, host/ and exp/ create or derive a secret or a random value: (file under writepath/, text on the
# line, what it is, how this scanner covers it). tests/test_leak_scan.py greps for creation calls and fails on a line
# no entry names, and on an entry that names no line.
SITES = [
    ("wp/keys.py", "k = os.urandom(32).hex()", "subject key", "key store: subject_keys.key_hex"),
    ("wp/keys.py", "(tenant, os.urandom(32).hex())", "tenant key", "key store: tenant_keys.hkey_hex"),
    ("wp/keys.py", "return hmac.new(k, text.encode(), hashlib.sha256).hexdigest()", "tenant HMAC",
     "pseudonyms are not secret; over 'chain-key:v1' it is the chain key: derived from each tenant key"),
    ("wp/keys.py", "return [hmac.new(k, x.encode(), hashlib.sha256)", "tenant HMAC, batch", "not secret: pseudonyms"),
    ("wp/keys.py", "n = os.urandom(12)", "AES-GCM nonce", "not secret: stored beside the ciphertext"),
    ("wp/keys.py", "return hmac.new(key, canonical.encode()", "request fingerprint", "not secret: a MAC output"),
    ("wp/seal.py", "return hmac.new(ks.chain_key(tenant)", "chain MAC", "not secret: a MAC output (its key is derived)"),
    ("wp/log.py", "return hmac.new(key, h.encode()", "event MAC", "not secret: a MAC output"),
    ("wp/setup.py", "pw = secrets.token_urlsafe(32)", "PG login role password", "file: secrets/pgpass.<role>"),
    ("wp/setup.py", "encrypt_password(pw.encode()", "PG SCRAM verifier", "memory only: salted, never written"),
    ("wp/es_security.py", "bin/elasticsearch-reset-password", "elastic password", "file: secrets/es_elastic_password"),
    ("wp/es_keys.py", '"/_security/api_key", {"name": f"wp16096-{role}"', "role API keys",
     "file: secrets/es_api_key.<role>.json"),
    ("wp/es_keys.py", '"/_security/api_key", {"name": "wp16096-admin"', "admin API key", "file: secrets/es_api_key.json"),
    ("wp/es_keys.py", "pw = _s.token_urlsafe(32)", "broker user password", "file: secrets/es_broker_password"),
    ("wp/es_keys.py", '"/_security/user/{B.BROKER_USER}", {"password": pw', "broker user password set",
     "file: secrets/es_broker_password"),
    ("wp/broker.py", '"POST", "/_security/api_key", {"name": name', "broker-minted writer API keys",
     "memory only: the broker checks its own output with find()"),
    ("host/bootstrap.py", "salt = secrets.token_bytes(16)", "SCRAM salt", "not secret: part of the verifier"),
    ("host/bootstrap.py", 'client_key = hmac.new(salted, b"Client Key"', "SCRAM client key", "memory only: never written"),
    ("host/bootstrap.py", 'server = hmac.new(salted, b"Server Key"', "SCRAM server key",
     "memory only: written into PG's catalog, not into a file"),
    ("host/bootstrap.py", '_write_secret(sec / "pg_password", secrets.token_urlsafe(32))', "PG admin password",
     "file: secrets/pg_password and secrets/pgpass"),
    ("host/bootstrap.py", "_write_secret(pf, secrets.token_urlsafe(32))", "legacy role passwords",
     "file: secrets/pw_emlab, secrets/pw_kms and oldhome/pgpass"),
    ("exp/h_probe_es.py", 'by.req("POST", "/_security/api_key", body', "probe API keys",
     "memory only: the probe checks its own output with find()"),
    ("exp/h_probe_es.py", "doc_id = doc_id or secrets.token_hex(8)", "probe document id", "not secret"),
    ("exp/h_probe_es.py", "pw = secrets.token_urlsafe(32)", "probe broker password", "file: secrets/es_broker_password"),
    ("exp/h_probe_es.py", '"/_security/user/{USER}", {"password": pw', "probe broker password set",
     "file: secrets/es_broker_password"),
    ("exp/disk_gate.py", 'return "s-" + os.urandom(4).hex()', "stop id", "not secret"),
    ("exp/disk_gate.py", '"-" + os.urandom(4).hex()', "run id", "not secret"),
]


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
    try:
        t = v.decode("utf-8")
        if any(ord(c) > 127 for c in t):        # ASCII text in UTF-16 is found by the NUL-free view
            reps += [("utf16le", t.encode("utf-16-le")), ("utf16be", t.encode("utf-16-be"))]
    except UnicodeDecodeError:
        pass
    return [(c, r) for c, r in reps if r]


def _keystore_reps(hexkey):
    """A key-store or derived key: its hex text, its raw bytes and their encodings."""
    raw = bytes.fromhex(hexkey)
    return [(c, r) for c, r in representations(raw) + [("b64-of-hex", x) for _, x in _b64_cores(hexkey.encode())]]


def derived_keys(tenant_hex):
    """The chain key of each tenant key (wp/keys.py chain_key), as the hex the key service sends."""
    return [hmac.new(bytes.fromhex(k), CHAIN_LABEL, hashlib.sha256).hexdigest() for k in tenant_hex]


# ------------------------------------------------------------------------------------------------------- walking
def _walk(roots, skips):
    """(path, kind) for every entry under the roots, kind "file", "dir" or "other". Links are followed when they stay
    inside the roots (each directory once); everything not searched is appended to skips as (reason, path)."""
    rroots = [os.path.realpath(r) for r in roots]

    def inside(rp):
        return any(rp == r or rp.startswith(r.rstrip(os.sep) + os.sep) for r in rroots)
    seen = set()
    for top in roots:
        try:
            st = os.stat(top)
        except OSError as e:
            skips.append((f"requested root does not exist or cannot be read ({type(e).__name__})", top))
            continue
        if stat.S_ISREG(st.st_mode):
            yield top, "file"
            continue
        if not stat.S_ISDIR(st.st_mode):
            skips.append(("requested root is not a regular file or directory", top))
            continue
        stack = [top]
        while stack:
            d = stack.pop()
            try:
                dst = os.stat(d)
            except OSError as e:
                skips.append((f"directory cannot be read ({type(e).__name__})", d))
                continue
            if (dst.st_dev, dst.st_ino) in seen:
                continue
            seen.add((dst.st_dev, dst.st_ino))
            try:
                names = sorted(os.listdir(d))
            except OSError as e:
                skips.append((f"directory cannot be listed ({type(e).__name__})", d))
                continue
            for n in names:
                p = os.path.join(d, n)
                try:
                    st = os.stat(p)
                except OSError as e:
                    yield p, "other"
                    skips.append(("dangling link" if os.path.islink(p) else f"cannot be read ({type(e).__name__})", p))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    yield p, "dir"
                    if os.path.islink(p) and not inside(os.path.realpath(p)):
                        skips.append(("link to a directory outside the scanned roots: not followed", p))
                    else:
                        stack.append(p)
                elif stat.S_ISREG(st.st_mode):
                    yield p, "file"
                else:
                    yield p, "other"
                    skips.append(("not a regular file (FIFO, socket or device): not read", p))


def _read(p, skips):
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        skips.append((f"cannot be read ({type(e).__name__})", p))
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            skips.append(("not a regular file: not read", p))
            return None
        if st.st_size > MAX_BYTES:
            skips.append((f"over the size limit ({st.st_size} > {MAX_BYTES} bytes): not read", p))
            return None
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            skips.append((f"grew over the size limit ({MAX_BYTES} bytes) while read: not searched", p))
            return None
        return data
    except OSError as e:
        skips.append((f"cannot be read ({type(e).__name__})", p))
        return None
    finally:
        os.close(fd)


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


def _file_values(fn, d_name, raw, inv, rel):
    vals = []
    if fn.startswith("pgpass") or (d_name == "oldhome" and fn == "pgpass"):
        for user, pw in _pgpass_entries(raw.decode("utf-8", "replace")):
            vals.append((pw, "frag"))
            inv["controls"].append(user.encode())
    elif fn.endswith((".crt", ".pem")) and b"BEGIN CERTIFICATE" in raw and b"PRIVATE KEY" not in raw:
        inv["coverage"].append(("skipped {}: public certificate", rel))
        return None
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
    return vals


def _psql_keys(c, conf, q):
    try:
        r = subprocess.run(["docker", "exec", c, "psql", "-U", conf("pg_user", "wp"), "-d", conf("keys_db", "wp_keys"),
                            "-At", "-v", "ON_ERROR_STOP=1", "-c", q], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScanError(f"key store not readable ({type(e).__name__}); pass --no-keystore REASON to scan without it") from None
    if r.returncode != 0:
        raise ScanError(f"key store not readable (psql exit {r.returncode}); pass --no-keystore REASON to scan without it")
    words = r.stdout.decode("utf-8", "replace").split()
    keys = [x for x in words if re.fullmatch(r"[0-9a-fA-F]{32,}", x) and len(x) % 2 == 0]
    if len(keys) != len(words):
        raise ScanError(f"the key store returned {len(words) - len(keys)} value(s) that are not hex keys")
    return [k.lower() for k in keys]


def load_inventory(home, keystore=None, no_keystore=None, secrets_dir=None):
    """{"file": [(source, value bytes, mode)], "keystore": [(source, hex)], "controls": [bytes],
    "coverage": [(format, part...)], "errors": [(reason, path)]}. Raises ScanError only when there is nothing to scan
    against; a source that cannot be read is an error entry (the caller prints it redacted and exits 2)."""
    home = pathlib.Path(home)
    dirs = [d for d in sorted(home.iterdir()) if d.is_dir() and d.name.startswith("secrets")] if home.is_dir() else []
    if secrets_dir and pathlib.Path(secrets_dir).resolve() not in [d.resolve() for d in dirs]:
        dirs.append(pathlib.Path(secrets_dir))
    if (home / "oldhome").is_dir():
        dirs.append(home / "oldhome")
    inv = {"file": [], "keystore": [], "controls": [], "coverage": [], "errors": []}
    if not dirs:
        raise ScanError("no secrets directory under WP_HOME")
    for d in dirs:
        skips = []
        for p, kind in _walk([str(d)], skips):
            if kind != "file":
                continue
            raw = _read(p, skips)
            if raw is None:
                continue
            fn, rel = os.path.basename(p), os.path.relpath(p, home)
            try:
                vals = _file_values(fn, d.name, raw, inv, rel)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                inv["errors"].append((f"secret source cannot be parsed ({type(e).__name__})", rel))
                continue
            if vals is None:
                continue
            kept = [(v, m) for v, m in vals if len(v) >= MIN_LEN]
            inv["file"] += [(rel, v.encode(), m) for v, m in kept]
            short = len(vals) - len(kept)
            inv["coverage"].append((f"source {{}}: {len(kept)} value(s)" +
                                    (f", {short} under {MIN_LEN} chars not inventoried" if short else ""), rel))
        inv["errors"] += [(f"secret source not read: {why}", os.path.relpath(p, home)) for why, p in skips]
    if no_keystore:
        inv["coverage"].append(("key store and derived chain keys: NOT COVERED ({})", no_keystore))
    else:
        conf = lambda n, d=None: ((home / "conf" / n).read_text().strip() if (home / "conf" / n).exists() else d)  # noqa: E731
        c = keystore or conf("pg_container")
        if not c:
            raise ScanError("key store: no PG container named (conf/pg_container or --keystore-container)")
        subject = _psql_keys(c, conf, "select key_hex from subject_keys")
        tenant = _psql_keys(c, conf, "select hkey_hex from tenant_keys")
        inv["keystore"] = ([("keystore", k) for k in subject + tenant] +
                           [("derived chain key", k) for k in derived_keys(tenant)])
        inv["coverage"].append((f"key store: {len(subject)} subject and {len(tenant)} tenant key(s), {len(tenant)} "
                                "derived chain key(s); keys destroyed before this scan cannot be checked",))
    if not inv["file"] and not inv["keystore"]:
        raise ScanError("no secret value inventoried: nothing to compare against")
    mem = sorted({what for _, _, what, how in SITES if how.startswith("memory only")})
    inv["coverage"].append((f"creation sites: {len(SITES)} listed; NOT COVERED (memory only, never written by the "
                            f"stream): {', '.join(mem)}; values rotated or destroyed before this scan",))
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


def _gunzip(data, skips, where):
    """As much of a gzip stream as decompresses, up to MAX_BYTES; damage and the limit are listed in skips."""
    out = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            while True:
                chunk = g.read1(1 << 20)
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_BYTES:
                    skips.append((f"decompressed content over the size limit ({MAX_BYTES} bytes): the rest not searched",
                                  where))
                    del out[MAX_BYTES:]
                    break
    except (OSError, EOFError, zlib.error) as e:
        skips.append((f"gzip stream damaged after {len(out)} decompressed bytes ({type(e).__name__}): the rest not "
                      "searched", where))
    return bytes(out)


def views(data, skips=None, where="data", depth=0):
    """The normalised views of data (module docstring). Anything not searched goes to skips; with skips None, it raises
    ScanError instead."""
    own = [] if skips is None else skips
    out = []

    def add(v):
        if v not in out:
            out.append(v)
    bases = [data] + ([data.replace(b"\0", b"")] if b"\0" in data else [])
    for b in bases:
        add(b)
        cur = b.translate(None, WS)
        add(cur)
        for _ in range(ROUNDS):
            nxt = cur
            if b"\\" in nxt:
                nxt = _unescape(nxt).translate(None, WS)
            if b"%" in nxt:
                nxt = urllib.parse.unquote_to_bytes(nxt).translate(None, WS)
            if nxt == cur:
                break
            add(nxt)
            cur = nxt
    if data[:2] == b"\x1f\x8b":
        if depth >= GZ_DEPTH:
            own.append((f"gzip nested more than {GZ_DEPTH} levels: the inner stream not searched", where))
        else:
            out += views(_gunzip(data, own, where), own, where, depth + 1)
    if skips is None and own:
        raise ScanError("; ".join(r for r, _ in own))
    return out


def find(data, values):
    """For an issuing process: the representation classes of `values` (secrets in memory) found in `data`. Raises
    ScanError when part of data cannot be searched (a damaged or oversized gzip stream)."""
    idx = Index({"file": [("in-process", v.encode() if isinstance(v, str) else v, "frag") for v in values], "keystore": []})
    return sorted({c for v in views(data) for c, _ in idx.find(v)})


def scan(roots, idx):
    """({path: {(class, source)}}, [(reason, path)] not searched, files scanned). Names are searched as well as
    content: a name holding a candidate is copied with the results."""
    hits, skips, scanned = {}, [], 0
    for p, kind in _walk(roots, skips):
        h = set()
        for v in views(os.fsencode(p), skips, p):
            h |= {("name:" + c, s) for c, s in idx.find(v)}
        if kind == "file":
            data = _read(p, skips)
            if data is not None:
                scanned += 1
                for v in views(data, skips, p):
                    h |= idx.find(v)
        if h:
            hits[p] = h
    return hits, skips, scanned


class Redactor:
    """Prints a path, source name or reason only after checking it against the inventory."""

    def __init__(self, idx):
        self.idx, self.n = idx, {}

    def holds(self, s):
        try:
            return any(self.idx.find(v) for v in views(os.fsencode(s) if isinstance(s, str) else s))
        except ScanError:
            return True

    def part(self, s, kind="path"):
        s = str(s)
        return f"<redacted {kind} #{self.n.setdefault(s, len(self.n) + 1)}>" if self.holds(s) else s

    def line(self, fmt, *parts, kind="path"):
        out = fmt.format(*(self.part(p, kind) for p in parts))
        return f"<redacted line #{self.n.setdefault(out, len(self.n) + 1)}>" if self.holds(out) else out


# ------------------------------------------------------------------------------------------------------ self-test
def self_test():
    """Plant FAKE values in every representation; all must be found, no control may be, and damaged input must be
    listed. Returns (found, planted, control hits, damaged input listed). No value is printed."""
    pw = _rnd.token_urlsafe(32)                                     # like the PG and broker passwords
    espw = "".join(_rnd.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789~!@#$%^&*-_=+?") for _ in range(20))
    kid, key = _rnd.token_urlsafe(15)[:20], _rnd.token_urlsafe(16)[:22]
    enc = base64.b64encode(f"{kid}:{key}".encode()).decode()
    kms, tkey = _rnd.token_bytes(32), _rnd.token_bytes(32)
    chain = bytes.fromhex(derived_keys([tkey.hex()])[0])
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
        "chain_hex": json.dumps({"ok": chain.hex()}), "chain_base64": b64(chain),
        "utf16le": f"x {pw}".encode("utf-16-le"), "utf16be_odd": b"\x01" + f"x {espw}".encode("utf-16-be"),
        "percent_json_escapes": urllib.parse.quote("".join("\\u%04x" % ord(c) for c in key), safe=""),
        "gzip": gzip.compress(f"line\n{pw}\n".encode()), "gzip_in_gzip": gzip.compress(gzip.compress(pw.encode())),
        f"name-{pw[3:24]}": "nothing inside",
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
        bad = pathlib.Path(tmp) / "bad"
        bad.mkdir()
        (bad / "damaged.gz").write_bytes(b"\x1f\x8b\x08\x00" + _rnd.token_bytes(64))
        inv = load_inventory(home, no_keystore="self-test")
        inv["keystore"] = [("keystore", kms.hex()), ("keystore", tkey.hex())] + \
                          [("derived chain key", k) for k in derived_keys([tkey.hex()])]
        idx = Index(inv)
        hits, skips, _ = scan([str(res)], idx)
        found = sum(1 for n in planted if str(res / n) in hits)
        damaged = scan([str(bad)], idx)[1]
        return found, len(planted), int(str(res / "controls") in hits), len(damaged) == 1 and not skips
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
    found, planted, ctl, damaged = self_test()
    print(f"self-test: {found}/{planted} planted representations found, {ctl} control hit(s), damaged input "
          f"{'listed' if damaged else 'NOT LISTED'}")
    if found != planted or ctl or not damaged:
        print("leak_scan: SELF-TEST FAILED, no scan made")
        return 2
    try:
        inv = load_inventory(home, ks, nks, os.environ.get("WP_SECRETS_DIR"))
    except ScanError as e:
        print(f"leak_scan: CANNOT SCAN: {e}")        # these messages hold no path or value
        return 2
    except OSError as e:
        print(f"leak_scan: CANNOT SCAN: WP_HOME cannot be listed ({type(e).__name__})")
        return 2
    idx = Index(inv)
    R = Redactor(idx)
    for c in inv["coverage"]:
        print(R.line(c[0], *c[1:], kind="text" if "NOT COVERED ({})" in c[0] else "path"))
    for why, p in inv["errors"]:
        print(R.line("SOURCE {}: " + why, p))
    if inv["errors"]:
        print(f"leak_scan: CANNOT SCAN: {len(inv['errors'])} secret source(s) could not be read; no scan made")
        return 2
    print(f"candidate values: {len(inv['file'])} from files, {len(inv['keystore'])} from the key store and derived")
    print(f"size limit: {MAX_BYTES} bytes per file and per decompressed stream")
    hits, skips, scanned = scan(dirs, idx)
    print(f"files scanned: {scanned} not scanned: {len(skips)} entries with a secret value: {len(hits)}")
    for why, p in skips:
        print(R.line("NOT SCANNED {}: " + why, p))
    for p, h in sorted(hits.items()):
        srcs = sorted({s for _, s in h})
        print(R.line("HIT {} " + f"{len(h)} classes=" + ",".join(sorted({c for c, _ in h})) + " sources=" +
                     ",".join("{}" for _ in srcs), p, *srcs))
    print(LIMITS)
    return 1 if hits else (2 if skips or not scanned else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
