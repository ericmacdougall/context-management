"""The seal authority: "sealed" implies "scanned" (DESIGN-NOTES "Seal authority" A1 to A8; main's option (b)).

Before this module wp_app held every capability a write needs (subject keys, the tenant HMAC, INSERT on events), so the
mentions, scan version, matcher and secret-scan version of an event were the writer's word (review H F14). Now ONE
service outside wp_app holds the seal key; it admits, secret-scans, matches, encrypts and attests each event, and the
store (events_guard, quarantine_guard) refuses any row whose attestation is missing (WPS40) or does not verify (WPS41).

  server   python -m wp.sealer serve SOCKET   runs as the authority's own identity (lab: uid 20005, its own 0700
           directory holding only seal_key and pgpass.wp_sealer). It reuses the key service's transport exactly
           (wp/keysvc.py Server: unix socket, kernel peer uid by SO_PEERCRED, fail-closed allow list, here
           WP_SEALER_ALLOW_UIDS); it is a client of the key service (WP_KEYSVC) and of Postgres as wp_sealer (SELECT on
           identifier_epochs and subject_identifiers only). One operation: seal.
  client   Client(SOCKET).seal(...) has the same signature and return value as Authority.seal.
  choice   sealer_for(conn, ks): WP_SEALER set means the socket ONLY (never an in-process authority, even when a key
           file is readable); otherwise an in-process Authority with the key file WP_SECRETS_DIR/seal_key (operator
           tools and experiments that hold every credential, as keysvc.key_store()); neither is SealUnavailable.

The attestation is HMAC-SHA256 under an independent random 32-byte key (NOT derived from the tenant key: the key
service is an HMAC oracle for that key and hands out chain_key) over a length-prefixed encoding ("<UTF-8 octets>:<value>"
per field, so no separator can be forged) of: "wp-seal-v1", key id, tenant, event id, subject id, sha256 hex of the
ciphertext, identifier-set version, matcher id, secret-scan version, the mentions (count, then each), lease scope,
lease epoch, batch id, index in batch, batch size. The store holds the key's HMAC pads in the private schema wp_seal and
computes sha256(opad || sha256(ipad || M)) itself (RFC 2104). The log names counts, a refusal code and seconds, plus
short hashes of the tenant and batch (caller bytes never reach the log); never content, ciphertext, keys, MACs,
identifiers, subject ids or event ids."""
import hashlib
import hmac
import json
import os
import pathlib
import socket
import stat
import sys
import time

from . import keys as K, mentions as M, secrets as SEC

VERSION = "wp-seal-v1"
OPS = ("seal",)
BLOCK = 64                                   # SHA-256 block size (RFC 2104 B)
KEY_FILE = "seal_key"
_REFUSALS = {c.code: c for c in (M.ContentTooLarge, M.MatcherBudgetExceeded, M.CallBudgetExceeded)}


class SealError(Exception):
    """The authority answered with an error that is not a refusal of the batch."""


class SealUnavailable(SealError):
    """No authority could be reached (no socket, connection refused or closed, peer refused, no key). Nothing is stored:
    the store requires the attestation, so there is no fallback."""


# ------------------------------------------------------------------ the attestation
def encode(fields):
    """Length-prefixed UTF-8: b"<octets>:<value>" per field. A non-text field (None included) is refused, never skipped."""
    out = []
    for f in fields:
        if not isinstance(f, str):
            raise TypeError("an attested field is not text")
        b = f.encode("utf-8")
        out.append(b"%d:" % len(b) + b)
    return b"".join(out)


def _int(v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise TypeError("an attested number is not a non-negative int")
    return str(v)


def message(key_id, tenant, event_id, subject_id, content_digest, scan, matcher, secret_scan, mentions, lease_scope,
            lease_epoch, batch_id, index, count):
    if not isinstance(mentions, (list, tuple)):
        raise TypeError("mentions is not a list")
    return encode([VERSION, key_id, tenant, event_id, subject_id, content_digest, _int(scan), matcher, secret_scan,
                   _int(len(mentions)), *mentions, lease_scope, _int(lease_epoch), batch_id, _int(index), _int(count)])


def mac(key, msg):
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def pads(key):
    """(ipad, opad) as the store keeps them: the key zero-padded to 64 bytes XOR 0x36 / 0x5c. They ARE the key."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("a seal key is 32 bytes")
    k = key.ljust(BLOCK, b"\0")
    return bytes(b ^ 0x36 for b in k), bytes(b ^ 0x5C for b in k)


def load_key(path):
    """(key_id, key) from a JSON file {"key_id", "key": hex} that only its owner can read (mode 0600 or stricter)."""
    p = pathlib.Path(path)
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode):
        raise PermissionError("the seal key file is not a regular file")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise PermissionError("the seal key file is readable by others (mode must be 0600)")
    d = json.loads(p.read_text())
    kid, key = d.get("key_id"), bytes.fromhex(d.get("key", ""))
    if not isinstance(kid, str) or not kid or len(key) != 32:
        raise ValueError("malformed seal key file")
    return kid, key


def new_key_file(path):
    """Create the key file (0600, never overwritten); returns the key id. The key itself is never printed or logged."""
    import secrets
    kid = "seal-" + secrets.token_hex(8)                # a random label, not derived from the key
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps({"key_id": kid, "key": secrets.token_bytes(32).hex()}).encode())
    finally:
        os.close(fd)
    return kid


def _tag(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ the authority
class Authority:
    """Admits, secret-scans, matches (version first), encrypts and attests one batch. `conn` reads identifier_epochs and
    subject_identifiers (deployed: as wp_sealer); `ks` is a KeyStore (deployed: the key service's client)."""

    def __init__(self, conn, ks, key_id, key, log=None):
        if not isinstance(key, bytes) or len(key) != 32 or not key_id:
            raise ValueError("a seal key is 32 bytes with a key id")
        self.conn, self.ks, self.key_id, self._key, self.log = conn, ks, key_id, key, log

    def _note(self, tenant, batch_id, events, result, t0):
        if self.log:
            self.log("seal tenant#%s batch#%s events=%d bytes=%d result=%s seconds=%.3f" % (
                _tag(tenant), _tag(batch_id), len(events), sum(len(str(e.get("content", "")).encode()) for e in events), result,
                time.monotonic() - t0))

    def seal(self, tenant, lease_scope, lease_epoch, batch_id, events):
        """events: [{event_id, subject_id, content}]. Returns per event, in order: {event_id, content_enc,
        content_digest, mentions, scan, matcher, secret_scan, seal_key_id, seal_mac, seal_batch, seal_index,
        seal_count}. Raises WriteRefused (WPM01 to WPM03) or SecretDetected before any key is used."""
        t0 = time.monotonic()
        for v in (tenant, lease_scope, batch_id):
            if not isinstance(v, str) or not v:
                raise ValueError("tenant, lease scope and batch id are non-empty text")
        _int(lease_epoch)
        if not isinstance(events, list) or not events:
            raise ValueError("a batch is a non-empty list")
        for e in events:
            if not (isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("event_id", "subject_id", "content"))
                    and e["event_id"] and e["subject_id"]):
                raise ValueError("an event is {event_id, subject_id, content} of text")
        if len({e["event_id"] for e in events}) != len(events):
            raise ValueError("event ids repeat within the batch")
        texts = [e["content"] for e in events]
        try:
            M.admit(texts)                                   # 1. admission (b1, review H): nothing else runs on refusal
            sv = SEC.check(texts, "event")                   # 2. the secret scanner (row 19)
            mid = M.matcher_id()
            version, found = M.scan(self.conn, self.ks, tenant, texts)   # 3. version and matcher first, then identifiers
        except M.WriteRefused as ex:
            self._note(tenant, batch_id, events, ex.code, t0)
            raise
        except SEC.SecretDetected:
            self._note(tenant, batch_id, events, "secret", t0)
            raise
        keys = self.ks.keys(tenant, sorted({e["subject_id"] for e in events}), create=True)
        n, out = len(events), []
        for i, (e, f) in enumerate(zip(events, found)):
            enc = bytes(K.seal(keys[e["subject_id"]], tenant, e["subject_id"], e["event_id"], e["content"]))   # 4.
            dg = hashlib.sha256(enc).hexdigest()
            men = sorted({x for x in f if x != e["subject_id"]})
            m = message(self.key_id, tenant, e["event_id"], e["subject_id"], dg, version, mid, sv, men, lease_scope,
                        lease_epoch, batch_id, i, n)                                                           # 5.
            out.append({"event_id": e["event_id"], "content_enc": enc, "content_digest": dg, "mentions": men, "scan": version,
                        "matcher": mid, "secret_scan": sv, "seal_key_id": self.key_id, "seal_mac": mac(self._key, m),
                        "seal_batch": batch_id, "seal_index": i, "seal_count": n})
        self._note(tenant, batch_id, events, "ok", t0)
        return out


def dispatch(auth, op, a):
    """One request against an Authority (the key service transport's dispatch_fn). Ciphertext travels as hex."""
    if op != "seal":
        return {"error": "unknown_op"}
    try:
        rows = auth.seal(a.get("tenant"), a.get("lease_scope"), a.get("lease_epoch"), a.get("batch_id"), a.get("events"))
    except M.WriteRefused as ex:
        return {"error": "WriteRefused", "code": ex.code, "index": getattr(ex, "index", None)}
    except SEC.SecretDetected as ex:
        return {"error": "SecretDetected", "message": str(ex)}      # detector names and item indexes only (wp/secrets.py)
    except K.SubjectErased as ex:
        return {"error": "SubjectErased", "subject": str(ex)}
    except M.MatcherMismatch:
        return {"error": "MatcherMismatch"}
    except (TypeError, ValueError):
        return {"error": "bad_request"}
    return {"ok": [dict(r, content_enc=r["content_enc"].hex()) for r in rows]}


class Client:
    """The authority over its socket. Same seal() as Authority; SealUnavailable when it cannot be reached."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("WP_SEALER") or ""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(self.path)
        except OSError as e:
            self.sock.close()
            raise SealUnavailable(f"seal authority unreachable ({type(e).__name__})") from None
        self.f = self.sock.makefile("rwb")

    def seal(self, tenant, lease_scope, lease_epoch, batch_id, events):
        req = {"op": "seal", "args": {"tenant": tenant, "lease_scope": lease_scope, "lease_epoch": lease_epoch, "batch_id": batch_id,
                                      "events": [{"event_id": e["event_id"], "subject_id": e["subject_id"], "content": e["content"]}
                                                 for e in events]}}
        try:
            self.f.write((json.dumps(req) + "\n").encode())
            self.f.flush()
            line = self.f.readline()
        except OSError as e:
            raise SealUnavailable(f"seal authority connection failed ({type(e).__name__})") from None
        if not line:
            raise SealUnavailable("the seal authority closed the connection")
        rep = json.loads(line)
        if "ok" in rep:
            return [dict(r, content_enc=bytes.fromhex(r["content_enc"])) for r in rep["ok"]]
        err = rep.get("error")
        if err == "WriteRefused" and rep.get("code") in _REFUSALS:
            raise _REFUSALS[rep["code"]]("refused by the seal authority", rep.get("index"))
        if err == "SecretDetected":
            raise SEC.SecretDetected(rep.get("message") or "secret-like content refused")
        if err == "SubjectErased":
            raise K.SubjectErased(rep.get("subject"))
        if err == "MatcherMismatch":
            raise M.MatcherMismatch("the tenant's matcher is not the authority's")
        if err == "peer_refused":
            raise SealUnavailable("the seal authority refused this peer uid")
        raise SealError(f"seal authority error {err}")

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()


def key_path():
    d = os.environ.get("WP_SECRETS_DIR")
    if d:
        return pathlib.Path(d) / KEY_FILE
    from . import config
    return config.SECRETS / KEY_FILE


def sealer_for(conn, ks):
    """The append path's sealer (A8 WHICH SEALER)."""
    if os.environ.get("WP_SEALER"):
        return Client()
    p = key_path()
    if not p.exists():
        raise SealUnavailable("no seal authority: WP_SEALER is not set and there is no seal key file")
    kid, key = load_key(p)
    return Authority(conn, ks, kid, key)


def allowed_uids():
    v = os.environ.get("WP_SEALER_ALLOW_UIDS", "")
    return frozenset(int(x) for x in v.split(",") if x.strip())


def serve(path):
    from . import db, keysvc
    allow = allowed_uids()
    if not allow:
        raise SystemExit("refusing to serve: WP_SEALER_ALLOW_UIDS lists no uid")
    kid, key = load_key(key_path())
    log = lambda s: print("sealer", s, flush=True)       # noqa: E731

    def make():
        return Authority(db.connect(application_name="wp-sealer", role=db.SEALER), keysvc.key_store(), kid, key, log=log)
    srv = keysvc.Server(path, allow, make_store=make, log=log, ops=OPS, dispatch_fn=dispatch)
    print(f"sealer serving uids={sorted(allow)} key_id={kid}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
        serve(sys.argv[2])
    else:
        raise SystemExit(__doc__)
