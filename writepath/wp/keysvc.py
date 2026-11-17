"""The key service as its own process and service identity (delta review D2, design path item 4).

Until this module the application process opened the key store itself as wp_kms (KeyService(db.keys_connect())), so
the application's identity could read the key store's credential. Now:

  server   python -m wp.keysvc serve SOCKET   runs under the key service's own identity, the only one that can read
           pgpass.wp_kms (WP_SECRETS_DIR holds just that file). It serves the KeyStore interface of wp/keys.py over a
           unix stream socket, one JSON request per line, one JSON reply per line, one key-store connection per client
           connection. A peer is identified by the kernel (SO_PEERCRED: the connecting process's uid), never by
           anything the peer sends; only uids listed in WP_KEYSVC_ALLOW_UIDS are served (fail closed: an empty or
           missing list serves nobody). It logs operation names, peer uids and counts, never a key, a text or an HMAC.
  client   Client(SOCKET) is a KeyStore with the same methods; the application gets one from key_store() when
           WP_KEYSVC names a socket.

What this does NOT change (stated in every receipt through describe()): the application still receives subject key
bytes, because encryption is app-side (wp/keys.py seal/open_); a KMS deployment would hand out wrapped data keys and
the same interface. The service trusts the peer uid for every tenant: tenant authority comes from authenticated
sessions (design path item 2), not from this service."""
import json
import os
import socket
import socketserver
import struct
import sys

from . import keys as K

OPS = ("keys", "tenant_hmac", "tenant_hmac_many", "chain_key", "is_destroyed", "key_exists", "destroy", "purge",
       "physical_pending", "describe")


def peer_uid(sock):
    """The connecting process's uid as the kernel reports it (Linux SO_PEERCRED: pid, uid, gid)."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def allowed_uids():
    v = os.environ.get("WP_KEYSVC_ALLOW_UIDS", "")
    return frozenset(int(x) for x in v.split(",") if x.strip())


def dispatch(ks, op, a):
    """One request against a KeyStore. Returns a JSON-able reply; key bytes travel as hex."""
    if op == "keys":
        try:
            got = ks.keys(a["tenant"], a["subjects"], create=bool(a.get("create")))
        except K.SubjectErased as e:
            return {"error": "SubjectErased", "subject": str(e)}
        return {"ok": {s: (k.hex() if k is not None else None) for s, k in got.items()}}
    if op == "tenant_hmac":
        return {"ok": ks.tenant_hmac(a["tenant"], a["text"])}
    if op == "tenant_hmac_many":
        return {"ok": ks.tenant_hmac_many(a["tenant"], a["texts"])}
    if op == "chain_key":
        return {"ok": ks.chain_key(a["tenant"]).hex()}
    if op == "is_destroyed":
        return {"ok": ks.is_destroyed(a["tenant"], a["subject"])}
    if op == "key_exists":
        return {"ok": ks.key_exists(a["tenant"], a["subject"])}
    if op == "destroy":
        ks.destroy(a["tenant"], a["subject"])
        return {"ok": True}
    if op == "purge":
        return {"ok": ks.purge(timeout_s=float(a.get("timeout_s", 60.0)))}
    if op == "physical_pending":
        return {"ok": [list(r) for r in ks.physical_pending(a.get("tenant"))]}
    if op == "describe":
        return {"ok": ks.describe()}
    return {"error": "unknown_op"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        uid = self.server.uid_of(self.request)
        if uid not in self.server.allow:
            self.wfile.write(b'{"error": "peer_refused"}\n')
            self.server.note(f"refused peer uid={uid}")
            return
        ks = None
        try:
            for line in self.rfile:
                label = "bad_request"       # review B3: the log names only a RECOGNIZED op, never caller bytes
                try:
                    try:
                        req = json.loads(line)
                    except ValueError:
                        req = None                  # unparseable: answered bad_request below, connection kept
                    op = req.get("op") if isinstance(req, dict) else None
                    args = req.get("args") if isinstance(req, dict) else None
                    if not isinstance(op, str) or op not in self.server.ops:
                        label = "unknown_op" if isinstance(req, dict) else "bad_request"
                        rep = {"error": label}
                    elif args is not None and not isinstance(args, dict):
                        label, rep = op, {"error": "bad_request"}
                    else:
                        label = op
                        ks = ks or self.server.make_store()
                        rep = self.server.dispatch(ks, op, args or {})
                except Exception as e:  # noqa: BLE001  (a failed request never kills the connection's peer handling)
                    rep = {"error": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None)}
                self.server.note(f"uid={uid} op={label} {'ok' if 'ok' in rep else rep.get('error')}")
                self.wfile.write((json.dumps(rep) + "\n").encode())
                self.wfile.flush()
        finally:
            if ks is not None and getattr(ks, "conn", None) is not None:
                ks.conn.close()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, allow, make_store=None, uid_of=peer_uid, log=None, ops=OPS, dispatch_fn=None):
        """ops / dispatch_fn: the service's operations (default: the key service's); the broker (wp/broker.py) reuses
        this transport and its kernel-uid check with its own."""
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, _Handler)
        os.chmod(path, 0o666)          # reachability only: the kernel-reported uid decides who is served
        self.allow, self.uid_of, self.log = frozenset(allow), uid_of, log
        self.make_store = make_store or K.KeyService
        self.ops, self.dispatch = frozenset(ops), dispatch_fn or dispatch

    def note(self, s):
        if self.log:
            self.log(s)


class ServiceError(Exception):
    pass


class Client(K.KeyStore):
    """KeyStore over the key service's socket. Same methods and return types as wp.keys.KeyService."""

    def __init__(self, path=None):
        self.path = path or os.environ["WP_KEYSVC"]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)
        self.f = self.sock.makefile("rwb")
        self._desc = None

    def _call(self, op, **a):
        self.f.write((json.dumps({"op": op, "args": a}) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ServiceError("key service closed the connection")
        rep = json.loads(line)
        if "ok" in rep:
            return rep["ok"]
        if rep.get("error") == "SubjectErased":
            raise K.SubjectErased(rep.get("subject"))
        raise ServiceError(rep.get("error") or "unknown")

    def keys(self, tenant, subjects, create=False):
        got = self._call("keys", tenant=tenant, subjects=sorted(set(subjects)), create=create)
        return {s: (bytes.fromhex(v) if v is not None else None) for s, v in got.items()}

    def tenant_hmac(self, tenant, text):
        return self._call("tenant_hmac", tenant=tenant, text=text)

    def tenant_hmac_many(self, tenant, texts):
        return self._call("tenant_hmac_many", tenant=tenant, texts=list(texts))

    def chain_key(self, tenant):
        return bytes.fromhex(self._call("chain_key", tenant=tenant))

    def is_destroyed(self, tenant, subject):
        return self._call("is_destroyed", tenant=tenant, subject=subject)

    def key_exists(self, tenant, subject):
        return self._call("key_exists", tenant=tenant, subject=subject)

    def destroy(self, tenant, subject):
        self._call("destroy", tenant=tenant, subject=subject)

    def purge(self, timeout_s=60.0, poll_s=0.25):
        return self._call("purge", timeout_s=timeout_s)

    def physical_pending(self, tenant=None):
        return [tuple(r) for r in self._call("physical_pending", tenant=tenant)]

    def describe(self):
        if self._desc is None:
            self._desc = dict(self._call("describe"), transport="separate key-service process over a unix socket; "
                              "peer identified by kernel uid (SO_PEERCRED); the application receives subject key bytes "
                              "(app-side encryption)")
        return self._desc

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()


def key_store():
    """The application's KeyStore: the key service's client when WP_KEYSVC names its socket, else the in-process
    stand-in (operator tools and the experiments that hold every credential)."""
    return Client() if os.environ.get("WP_KEYSVC") else K.KeyService()


def serve(path):
    allow = allowed_uids()
    if not allow:
        raise SystemExit("refusing to serve: WP_KEYSVC_ALLOW_UIDS lists no uid")
    srv = Server(path, allow, log=lambda s: print("keysvc", s, flush=True))
    print(f"keysvc serving uids={sorted(allow)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
        serve(sys.argv[2])
    else:
        raise SystemExit(__doc__)
