"""Defence in depth before results leave the lab host (GATE-DESIGN 7: clearance rests on teardown, runkit/clearance.py;
this scan runs on a set while its credentials still exist). Version 4 (review F FS1 to FS6); design and limits:
runkit/GATE-DESIGN.md sections 7.5 (v4), 5.7 (v3) and 3 (v2).

Its statement is exactly LIMITS below: these inventoried values were not found in these representations and views. It
never says a set holds no secrets.

Inventory, read at scan time and never printed. SITES below lists every Python call that creates or derives a secret
or a random value, by file, enclosing function and callee with its expected count, and how each is inventoried;
check_sites() finds the calls by parsing (import aliases and calls split across lines included) and reports any
call the table does not list, and any count that differs (tests/test_leak_scan.py). Shell and SQL text has a regex
lint only (LINT_SITES). Sources:
  WP_HOME/secrets*/, WP_SECRETS_DIR when set, and WP_HOME/oldhome/:
    pgpass*                the password field of every entry (pgpass escapes undone); user and host are not secret
    es_api_key*.json       the API key itself (decoded from `encoded`), and `encoded` whole; the key id is NOT secret
    *.crt / *.pem certs    public, skipped (counted)
    any other file         its stripped content; an unknown JSON file: each string value except id/name/expiration/type
  key store (the KMS stand-in): subject_keys.key_hex and tenant_keys.hkey_hex, read through `docker exec <PG container>
    psql` (conf pg_container, pg_user, keys_db) unless --no-keystore REASON is given; derived from each tenant key, the
    tenant chain key (HMAC-SHA256 over "chain-key:v1", wp/keys.py chain_key; the key service sends it as hex); and, when
    the database `keys` exists, the legacy write path's keys (keys.key, tenant_keys.hkey; old_repro runs that code).
Representations: the raw value, hex (both cases), base64 and base64url at all three byte alignments, UTF-16LE and
UTF-16BE of a non-ASCII value; for key-store and derived keys also the raw bytes and base64 of the hex.
Views of each file (FS1): the original bytes first; the same with NUL bytes removed (UTF-16 and UTF-32 text of ASCII
values); and from each, every result of applying {backslash unescape, percent-decode, whitespace removal} in any order
up to depth 3, every intermediate kept. Content starting with the gzip magic is decompressed (up to three levels) and
viewed the same way.
Fail closed: every requested root must exist; every directory the walk cannot list, file it cannot read, non-regular
file, dangling link, link to a directory outside the requested roots (not followed), damaged gzip stream and content
over the size limit (512 MiB per file and per decompressed stream; never searched in part silently) is listed and makes
the exit code 2. Every requested root's own path is searched before traversal (FS5). Nothing is excluded: a secret
source inside a scanned root is a hit.
Output (FS4): counts, and entries as `file #n` (walk order) and `source #k`; hits as `HIT file #n [name ]view <k>
offset <o> inventory v<i> class <c>`; reasons are the scanner's own text. No path, name or text from a scanned tree or
a waiver reason is printed. --map FILE writes the number-to-path table (and source names) to a new 0600 file, refused
inside a scanned root.
Exit 0: no hit and nothing left unscanned. 1: a hit. 2: cannot scan, or something was not scanned.
usage: python3 leak_scan.py WP_HOME DIR [DIR ...] [--keystore-container NAME] [--no-keystore REASON] [--map FILE]
library: representations(value); views(data); find(data, values) returns the representation classes of `values`
found in data (raises ScanError for content it cannot search completely); check_sites(root); unregistered(root)."""
import ast
import base64
import collections
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

VERSION = "4"
W = 16                         # fragment window: any 16 characters of a representation of a file-held secret
MIN_LEN = 8                    # shorter values are not inventoried (reported)
NOT_SECRET_JSON = {"id", "name", "expiration", "type"}
WS = b" \t\r\n\f\v"
MAX_BYTES = int(os.environ.get("LEAK_SCAN_MAX_BYTES") or 512 * 1024 ** 2)   # lowered by the tests only
GZ_DEPTH = 3
ROUNDS = 3
CHAIN_LABEL = b"chain-key:v1"
LIMITS = ("LIMITS v4 (GATE-DESIGN 7.5): found = 16 consecutive characters of one listed representation of a "
          "file-held secret (fragment class), or one complete representation of a key-store or derived key or of an "
          "encoded API key (complete class), in one view; views are the original bytes, NUL-free bytes, and "
          "unescape, percent-decode and whitespace removal in any order to depth 3, and gzip to depth 3. Not found: "
          "pieces shorter than 16 characters (of a complete-class value: anything short of the whole), encrypted "
          "content, compression other than gzip, gzip inside another encoding, deeper nesting, unlisted encodings, "
          "and values outside the inventory (memory-only keys, SCRAM internals, rotated or destroyed values). "
          "Clearance rests on teardown (runkit/clearance.py), not on this scan.")

# Every Python call in wp/, host/, exp/ and runkit/ (tests/, results/ and this scanner left out) that creates or derives
# a secret or a random value: (file under writepath/, enclosing function, callee, expected count, what it is, how this
# scanner covers it, whether it must register the value with wp/secret_sink.py). check_sites() compares the tree with
# this table by parsing it; unregistered() lists the register=True sites whose function does not call
# secret_sink.register (tests/test_result_sink_fence.py; hand-off H-1 to H-3). Not listed by design: uuid.uuid4
# (identifiers) and random.Random(seed) (reproducible test data); neither makes a credential.
MEM = "memory only: dies with the ES realm at teardown (GATE-DESIGN 7.4); no issuer-side check exists (FS2)"
SITES = [
    ("exp/disk_gate.py", "_new_id", "os.urandom", 1, "stop id", "not secret", False),
    ("exp/disk_gate.py", "_start", "os.urandom", 1, "run id", "not secret", False),
    ("exp/h_probe_es.py", "bulk", "secrets.token_hex", 1, "probe document id", "not secret", False),
    ("exp/h_probe_es.py", "mint", "http POST _security/api_key", 1, "probe API keys", MEM + "; sink H-4", True),
    ("exp/h_probe_es.py", "setup", "secrets.token_urlsafe", 1, "probe broker password",
     "file: secrets/es_broker_password", True),
    ("exp/h_probe_es.py", "setup", "http PUT _security/user", 1, "probe broker password set",
     "the password made in the same function", False),
    ("host/bootstrap.py", "_scram", "secrets.token_bytes", 1, "SCRAM salt", "not secret: part of the verifier", False),
    ("host/bootstrap.py", "_scram", "hmac.new", 2, "SCRAM client and server keys",
     "memory only: never written (the server key goes into PG's catalog)", True),
    ("host/bootstrap.py", "main", "secrets.token_urlsafe", 2, "PG admin password; legacy role passwords",
     "file: secrets/pg_password, secrets/pgpass; secrets/pw_emlab, secrets/pw_kms, oldhome/pgpass", True),
    ("host/bootstrap.py", "main", "str POSTGRES_PASSWORD", 1, "PG superuser password, passed by file",
     "not a creation: secrets/pg_password", False),
    ("wp/broker.py", "Broker._mint", "http POST _security/api_key", 1, "broker-minted writer API keys",
     MEM + "; sink H-2", True),
    ("wp/es_keys.py", "create", "http POST _security/api_key", 2, "role and admin API keys",
     "file: secrets/es_api_key.<role>.json, secrets/es_api_key.json", True),
    ("wp/es_keys.py", "create_broker", "secrets.token_urlsafe", 1, "broker user password",
     "file: secrets/es_broker_password", True),
    ("wp/es_keys.py", "create_broker", "http PUT _security/user", 1, "broker user password set",
     "the password made in the same function", False),
    ("wp/es_security.py", "_run_args", "str ELASTIC_PASSWORD", 1, "ES bootstrap password variable",
     "not a creation: removed from the recreated container's environment", False),
    ("wp/es_security.py", "enable", "str elasticsearch-reset-password", 1, "elastic password",
     "file: secrets/es_elastic_password", True),
    ("wp/keys.py", "KeyService.keys", "os.urandom", 1, "subject key", "key store: subject_keys.key_hex", True),
    ("wp/keys.py", "KeyService.tenant_hmac", "os.urandom", 1, "tenant key", "key store: tenant_keys.hkey_hex", True),
    ("wp/keys.py", "KeyService.tenant_hmac", "hmac.new", 1, "tenant HMAC; over 'chain-key:v1' the chain key",
     "pseudonyms are not secret; the chain key is derived from each tenant key (H-3 registers it in chain_key)", False),
    ("wp/keys.py", "KeyService.tenant_hmac_many", "os.urandom", 1, "tenant key, batch path",
     "key store: tenant_keys.hkey_hex", True),
    ("wp/keys.py", "KeyService.tenant_hmac_many", "hmac.new", 1, "tenant HMAC, batch", "not secret: pseudonyms", False),
    ("wp/keys.py", "req_hmac", "hmac.new", 1, "request fingerprint", "not secret: a MAC output", False),
    ("wp/keys.py", "seal", "os.urandom", 1, "AES-GCM nonce", "not secret: stored beside the ciphertext", False),
    ("wp/log.py", "event_hash", "hmac.new", 1, "event MAC", "not secret: a MAC output", False),
    ("wp/seal.py", "_mac", "hmac.new", 1, "chain MAC", "not secret: a MAC output (its key is derived)", False),
    ("wp/setup.py", "_ensure_roles", "secrets.token_urlsafe", 1, "PG login role password",
     "file: secrets/pgpass.<role>", True),
    ("wp/setup.py", "_ensure_roles", "encrypt_password", 1, "PG SCRAM verifier",
     "memory only: salted, never written; not a credential", False),
]
# Shell and SQL text: a regex lint (tests/test_leak_scan.py), not proof. The legacy write path (old_repro runs it from
# WP_HOME/oldsrc against this stream's PG, database keys) at its own commit.
LINT_SITES = [
    ("legacy:a13b524:systems/memsys/kms.sql", "k := encode(gen_random_bytes(32), 'hex');", "legacy subject keys",
     "key store: database keys, table keys (column key)"),
    ("legacy:a13b524:systems/memsys/kms.sql", "(t, encode(gen_random_bytes(32), 'hex')) on conflict do nothing",
     "legacy tenant keys", "key store: database keys, table tenant_keys (column hkey)"),
]
_CALLEES = {"secrets.token_bytes", "secrets.token_hex", "secrets.token_urlsafe", "secrets.choice", "secrets.randbelow",
            "secrets.randbits", "secrets.SystemRandom", "os.urandom", "os.getrandom", "random.SystemRandom",
            "random.random", "random.randint", "random.randrange", "random.choice", "random.choices", "random.sample",
            "random.shuffle", "random.uniform", "random.getrandbits", "random.randbytes", "hmac.new", "hmac.digest"}
_STRS = re.compile(r"elasticsearch-(reset-password|service-tokens|create-enrollment-token)|/dev/u?random|openssl rand|"
                   r"gen_random_bytes|gen_random_uuid|POSTGRES_PASSWORD|ELASTIC_PASSWORD")
_SEC = re.compile(r"_security/(api_key|service|user)\b")


def _text(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    if isinstance(n, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in n.values)
    return None


class _Sites(ast.NodeVisitor):
    """Calls by canonical callee (import aliases resolved), per enclosing function (Class.method, outer.inner)."""

    def __init__(self):
        self.alias, self.stack = {}, []
        self.out, self.regs = collections.Counter(), set()

    def fn(self):
        return ".".join(self.stack) or "<module>"

    def visit_Import(self, n):
        for a in n.names:
            self.alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]

    def visit_ImportFrom(self, n):
        for a in n.names:
            self.alias[a.asname or a.name] = f"{n.module or ''}.{a.name}".lstrip(".")

    def _scope(self, n):
        self.stack.append(n.name)
        self.generic_visit(n)
        self.stack.pop()
    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def _name(self, f):
        parts = []
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if not isinstance(f, ast.Name):
            return None
        return ".".join([self.alias.get(f.id, f.id)] + parts[::-1])

    def visit_Call(self, n):
        q = self._name(n.func)
        if q:
            if q in _CALLEES:
                self.out[(self.fn(), q)] += 1
            elif q.rsplit(".", 1)[-1] == "encrypt_password":
                self.out[(self.fn(), "encrypt_password")] += 1
            if q.endswith("secret_sink.register"):
                self.regs.add(self.fn())
        ts = [t for t in map(_text, list(n.args) + [k.value for k in n.keywords]) if t]
        verb = next((t for t in ts if t in ("POST", "PUT")), None)
        path = next((m.group(0) for m in map(_SEC.search, ts) if m), None)
        if verb and path:
            self.out[(self.fn(), f"http {verb} {path}")] += 1
        self.generic_visit(n)

    def visit_Constant(self, n):
        m = _STRS.search(n.value) if isinstance(n.value, str) else None
        if m:
            self.out[(self.fn(), "str " + m.group(0))] += 1

    def visit_JoinedStr(self, n):
        m = _STRS.search(_text(n) or "")
        if m:
            self.out[(self.fn(), "str " + m.group(0))] += 1
        for v in n.values:
            if not isinstance(v, ast.Constant):
                self.visit(v)


def _site_files(root):
    root = pathlib.Path(root)
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        if rel.split("/")[0] in ("tests", "results") or rel == "runkit/leak_scan.py":
            continue
        yield f, rel


def creation_sites(root):
    """({(file, function, callee): count}, {(file, function) that call secret_sink.register}, [problems])."""
    found, regs, probs = collections.Counter(), set(), []
    for f, rel in _site_files(root):
        v = _Sites()
        try:
            v.visit(ast.parse(f.read_bytes(), rel))
        except (SyntaxError, ValueError, OSError) as e:
            probs.append(f"cannot parse {rel} ({type(e).__name__}): its sites are unknown")
            continue
        for (fn, q), c in v.out.items():
            found[(rel, fn, q)] += c
        regs |= {(rel, fn) for fn in v.regs}
    return found, regs, probs


def check_sites(root):
    """Every difference between the tree's creation calls and SITES, as text naming file, function and callee."""
    found, _, probs = creation_sites(root)
    want = {(s[0], s[1], s[2]): s[3] for s in SITES}
    for k in sorted(set(found) | set(want)):
        n, m = want.get(k), found.get(k, 0)
        if n is None:
            probs.append(f"unlisted: {k[0]} {k[1]} {k[2]} x{m}")
        elif m != n:
            probs.append(f"count: {k[0]} {k[1]} {k[2]} expected {n} found {m}")
    return probs


def unregistered(root):
    """The register=True sites whose enclosing function does not call secret_sink.register."""
    _, regs, _ = creation_sites(root)
    return [f"{s[0]} {s[1]} {s[2]} ({s[4]})" for s in SITES if s[6] and (s[0], s[1]) not in regs]


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


def _psql(c, conf, q, db=None):
    try:
        r = subprocess.run(["docker", "exec", c, "psql", "-U", conf("pg_user", "wp"), "-d", db or conf("keys_db", "wp_keys"),
                            "-At", "-v", "ON_ERROR_STOP=1", "-c", q], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScanError(f"key store not readable ({type(e).__name__}); pass --no-keystore REASON to scan without it") from None
    if r.returncode != 0:
        raise ScanError(f"key store not readable (psql exit {r.returncode}); pass --no-keystore REASON to scan without it")
    return r.stdout.decode("utf-8", "replace").split()


def _psql_keys(c, conf, q, db=None):
    words = _psql(c, conf, q, db)
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
        if _psql(c, conf, "select datname from pg_database where datname = 'keys'") == ["keys"]:
            legacy = (_psql_keys(c, conf, "select key from keys", db="keys") +
                      _psql_keys(c, conf, "select hkey from tenant_keys", db="keys"))
            inv["keystore"] += [("legacy key store", k) for k in legacy]
            inv["coverage"].append((f"legacy key store (database keys, old_repro): {len(legacy)} key(s)",))
        else:
            inv["coverage"].append(("legacy key store (database keys, old_repro): absent",))
    if not inv["file"] and not inv["keystore"]:
        raise ScanError("no secret value inventoried: nothing to compare against")
    mem = sorted({s[4] for s in SITES if s[5].startswith("memory only")})
    inv["coverage"].append((f"creation sites: {len(SITES)} listed; NOT COVERED (memory only, never written by the "
                            f"stream): {', '.join(mem)}; values rotated or destroyed before this scan",))
    return inv


# ------------------------------------------------------------------------------------------------------- scanning
class Index:
    """Every representation of every inventoried value, by inventory id (v1, v2, ... in inventory order: file values,
    then key-store and derived keys). find() returns {(class, id): first offset}."""

    def __init__(self, inv):
        self.win, self.short, self.ids = {}, [], []
        for src, v, mode in inv["file"]:
            iid = f"v{len(self.ids) + 1}"
            self.ids.append((iid, src, "complete" if mode == "full" else "fragment"))
            for cls, r in representations(v):
                if len(r) < W:
                    self.short.append((r, cls, iid))
                elif mode == "full":
                    self.win.setdefault(r[:W], []).append((cls, iid, r))
                else:
                    for i in range(len(r) - W + 1):
                        self.win.setdefault(r[i:i + W], []).append((cls, iid, None))
        for src, hx in inv["keystore"]:
            iid = f"v{len(self.ids) + 1}"
            self.ids.append((iid, src, "complete"))
            for cls, r in _keystore_reps(hx):
                if len(r) >= W:
                    self.win.setdefault(r[:W], []).append((cls, iid, r))

    def find(self, data):
        hits = {}
        for r, cls, iid in self.short:
            o = data.find(r)
            if o >= 0:
                hits.setdefault((cls, iid), o)
        win = self.win
        for i in range(len(data) - W + 1):
            e = win.get(data[i:i + W])
            if e:
                for cls, iid, full in e:
                    if full is None or data.startswith(full, i):
                        hits.setdefault((cls, iid), i)
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


def _op_ws(b):
    w = b.translate(None, WS)
    return w if len(w) != len(b) else None


def _op_unescape(b):
    if b"\\" not in b:
        return None
    w = _unescape(b)
    return w if w != b else None


def _op_percent(b):
    if b"%" not in b:
        return None
    w = urllib.parse.unquote_to_bytes(b)
    return w if w != b else None


OPS = (_op_unescape, _op_percent, _op_ws)


def _closure(base, seen):
    """base, then every result of OPS applied in any order up to depth ROUNDS, each view once (depth first, so at most
    ROUNDS + 1 copies are held; a view first reached deeper is expanded again when reached shallower)."""
    def rec(v, depth):
        h = hashlib.sha256(v).digest()
        if h in seen and seen[h] <= depth:
            return
        new = h not in seen
        seen[h] = depth
        if new:
            yield v
        if depth < ROUNDS:
            for op in OPS:
                w = op(v)
                if w is not None:
                    yield from rec(w, depth + 1)
    yield from rec(base, 0)


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
    """The views of data (module docstring), the original bytes first, as a generator. Anything not searched goes to
    skips; with skips None, ScanError is raised at the end instead."""
    own = [] if skips is None else skips
    seen = {}
    for b in [data] + ([data.replace(b"\0", b"")] if b"\0" in data else []):
        yield from _closure(b, seen)
    if data[:2] == b"\x1f\x8b":
        if depth >= GZ_DEPTH:
            own.append((f"gzip nested more than {GZ_DEPTH} levels: the inner stream not searched", where))
        else:
            yield from views(_gunzip(data, own, where), own, where, depth + 1)
    if skips is None and own:
        raise ScanError("; ".join(r for r, _ in own))


def find(data, values):
    """For a process holding secrets in memory: the representation classes of `values` found in `data`, in every view.
    Raises ScanError when part of data cannot be searched (a damaged or oversized gzip stream)."""
    idx = Index({"file": [("in-process", v.encode() if isinstance(v, str) else v, "frag") for v in values], "keystore": []})
    return sorted({c for v in views(data) for c, _ in idx.find(v)})


def _search(idx, data, skips, where):
    """{(class, id): (view, offset)}: the first view and offset of each hit."""
    out = {}
    for k, v in enumerate(views(data, skips, where)):
        for key, o in idx.find(v).items():
            out.setdefault(key, (k, o))
    return out


def scan(roots, idx):
    """({path: {(part, class, id): (view, offset)}}, [(reason, path)] not searched, files scanned, [paths in walk
    order]). part is "name" or "content". Every requested root's own path is searched first (FS5), whatever the walk
    does with it; every entry's path is searched as well as its content."""
    hits, skips, scanned, order = {}, [], 0, []

    def note(p):
        if p not in hits:
            hits[p] = {}
            order.append(p)
    for r in roots:
        note(r)
        for (c, i), vo in _search(idx, os.fsencode(r), skips, r).items():
            hits[r].setdefault(("name", c, i), vo)
    for p, kind in _walk(roots, skips):
        note(p)
        for (c, i), vo in _search(idx, os.fsencode(p), skips, p).items():
            hits[p].setdefault(("name", c, i), vo)
        if kind == "file":
            data = _read(p, skips)
            if data is not None:
                scanned += 1
                for (c, i), vo in _search(idx, data, skips, p).items():
                    hits[p].setdefault(("content", c, i), vo)
    for _, p in skips:
        note(p)
    return {p: h for p, h in hits.items() if h}, skips, scanned, order


# ------------------------------------------------------------------------------------------------------ self-test
def self_test():
    """Plant FAKE values in every representation; all must be found, no control may be, and damaged input must be
    listed. Returns (found, planted, control hits, damaged input listed). No value is printed."""
    pw = _rnd.token_urlsafe(32)                                     # like the PG and broker passwords
    espw = "".join(_rnd.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789~!@#$%^&*-_=+?") for _ in range(20))
    pct = _rnd.token_urlsafe(8)[:8] + "%41" + _rnd.token_urlsafe(9)[:9]
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
        "escaped_percent_value": '{"p": "' + "".join("\\u%04x" % ord(c) for c in pct) + '"}',     # FS1
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
        (home / "secrets" / "pw_percent").write_text(pct + "\n")
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
        hits, skips, _, _ = scan([str(res)], idx)
        found = sum(1 for n in planted if str(res / n) in hits)
        damaged = scan([str(bad)], idx)[1]
        return found, len(planted), int(str(res / "controls") in hits), len(damaged) == 1 and not skips
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------- output
def _inside(p, roots):
    rp = os.path.realpath(p)
    return any(rp == r or rp.startswith(r.rstrip(os.sep) + os.sep) for r in map(os.path.realpath, roots))


def _write_map(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def main(argv):
    ks = nks = mp = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--keystore-container", "--no-keystore", "--map") and i + 1 < len(argv):
            if argv[i] == "--keystore-container":
                ks = argv[i + 1]
            elif argv[i] == "--no-keystore":
                nks = argv[i + 1]
            else:
                mp = argv[i + 1]
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
    if mp and (_inside(os.path.dirname(os.path.abspath(mp)) or ".", dirs) or os.path.lexists(mp)):
        print("leak_scan: CANNOT SCAN: --map must name a new file outside every scanned root")
        return 2
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
    srcs = {}

    def src(s):
        return f"source #{srcs.setdefault(s, len(srcs) + 1)}"
    for _, s, _ in idx.ids:
        src(s)
    for c in inv["coverage"]:
        if "NOT COVERED ({})" in c[0]:
            print(c[0].format("--no-keystore given; its reason is not printed"))
        else:
            print(c[0].format(*(src(p) for p in c[1:])) if len(c) > 1 else c[0])
    for why, p in inv["errors"]:
        print(f"SOURCE {src(p)}: {why}")
    if inv["errors"]:
        print(f"leak_scan: CANNOT SCAN: {len(inv['errors'])} secret source(s) could not be read; no scan made")
        return 2
    print(f"candidate values: {len(inv['file'])} from files, {len(inv['keystore'])} from the key store and derived; "
          f"inventory ids v1 to v{len(idx.ids)}")
    print(f"size limit: {MAX_BYTES} bytes per file and per decompressed stream")
    hits, skips, scanned, order = scan(dirs, idx)
    num = {p: n for n, p in enumerate(order, 1)}
    print(f"files scanned: {scanned} not scanned: {len(skips)} entries with a secret value: {len(hits)}")
    for why, p in skips:
        print(f"NOT SCANNED file #{num[p]}: {why}")
    for p in sorted(hits, key=num.get):
        for (part, c, iid), (k, o) in sorted(hits[p].items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
            print(f"HIT file #{num[p]} {'name ' if part == 'name' else ''}view {k} offset {o} inventory {iid} class {c}")
    if mp:
        _write_map(mp, {"leak_scan": VERSION, "files": {str(n): p for p, n in num.items()},
                        "sources": {str(n): s for s, n in srcs.items()},
                        "inventory": {iid: {"source": srcs[s], "class": k} for iid, s, k in idx.ids}})
        print("map: written (0600)")
    print(LIMITS)
    return 1 if hits else (2 if skips or not scanned else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
