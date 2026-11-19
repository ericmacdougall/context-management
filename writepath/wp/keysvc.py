"""The key service as its own process and service identity (delta review D2, design path item 4).

Until this module the application process opened the key store itself as wp_kms (KeyService(db.keys_connect())), so
the application's identity could read the key store's credential. Now:

  server   python -m wp.keysvc serve SOCKET   runs under the key service's own identity, the only one that can read
           pgpass.wp_kms (WP_SECRETS_DIR holds just that file). It serves the KeyStore interface of wp/keys.py over a
           unix stream socket, one JSON request per line, one JSON reply per line, one key-store connection per client
           connection. A peer is identified by the kernel (SO_PEERCRED: the connecting process's uid), never by
           anything the peer sends; only uids listed in WP_KEYSVC_ALLOW_UIDS are served (fail closed: an empty or
           missing list serves nobody). It logs operation names, peer uids and counts, never a key, a text or an HMAC.
           Bounds (review J N2; Server): a request line of at most line_limit bytes, finished within
           request_timeout_s of its first byte; at most max_per_uid connections per uid and max_conn in all (over a
           cap: "busy", closed before a thread starts). The broker and the seal authority use this transport.
           The socket is created mode 0666 by umask at bind, never chmod-ed by path (review J M13); the directory
           holding it must be writable only by the service (stated, not checked: the lab's is 0755, service-owned).
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
import threading
import time

from . import keys as K

OPS = ("keys", "tenant_hmac", "tenant_hmac_many", "chain_key", "is_destroyed", "key_exists", "destroy", "purge",
       "physical_pending", "describe")
# Transport bounds (review J N2; the broker shares them, the seal authority sets its own in wp/sealer.py). The client
# splits tenant_hmac_many and keys so that no request line it sends exceeds CLIENT_LINE (half of LINE_LIMIT).
LINE_LIMIT = 2 * 2**20
CLIENT_LINE = LINE_LIMIT // 2
REQUEST_TIMEOUT_S = 30.0
MAX_CONN_PER_UID = 32
MAX_CONN = 128
CLIENT_TIMEOUT_S = 120.0       # review J M11: a client waits at most this long for one reply (purge: plus its timeout_s)


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


class _TooLong(Exception):
    pass


class _Slow(Exception):
    pass


def _send_nowait(sock, data):
    """Best effort, never blocks (the accept loop's refusals): a fresh connection's send buffer takes a short line."""
    try:
        sock.setblocking(False)
        sock.send(data)
    except OSError:
        pass


class _Idle(Exception):
    pass


class _Handler(socketserver.BaseRequestHandler):
    """One connection (review J N2). Requests are read from the raw socket with a bound on the line (server.line_limit
    bytes including the newline) and a deadline from a request's first byte to its newline (server.request_timeout_s).
    Waiting for a request's first byte is bounded by server.idle_timeout_s when set (review 42cfd11 R2: the seal
    authority sets it; the key service and broker keep long-lived clients and do not), past which the connection is
    closed without a reply. A reply is sent under the request deadline. Over the bound or past the deadline: one error
    line, then the connection is closed. Parse, dispatch and reply encoding run in _serve under the server's work
    slots (when set), and nothing of a served request stays referenced while the connection waits for the next one
    (review 42cfd11 R2a). self.client_address is the peer uid that Server.process_request admitted."""

    store = None

    def _line(self, buf):
        srv, sock = self.server, self.request
        deadline = None
        idle = getattr(srv, "idle_timeout_s", None)
        while True:
            i = buf.find(b"\n")
            if i >= 0:
                if i + 1 > srv.line_limit:
                    raise _TooLong()
                return bytes(buf[:i + 1]), buf[i + 1:]
            if len(buf) >= srv.line_limit:
                raise _TooLong()
            if buf and deadline is None:
                deadline = time.monotonic() + srv.request_timeout_s
            if deadline is None:
                sock.settimeout(idle)
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _Slow()
                sock.settimeout(left)
            try:
                chunk = sock.recv(min(1 << 16, srv.line_limit - len(buf)))
            except socket.timeout:
                raise (_Slow() if deadline is not None else _Idle()) from None
            if not chunk:
                return None, buf
            buf += chunk

    def _send(self, data):
        self.request.settimeout(self.server.request_timeout_s)
        self.request.sendall(data)

    def _reply(self, rep):
        self._send((json.dumps(rep) + "\n").encode())

    def _serve(self, line):
        """One request line -> (reply bytes, log label, outcome). Everything parsed or produced is local to this call,
        so it is released before the connection waits again; under the server's work slots (when set) only that many
        requests are parsed, dispatched and encoded at once, and a request that cannot get a slot within
        server.queue_wait_s is answered busy (review 42cfd11 R2a)."""
        sem = self.server.work_semaphore()
        if sem is not None and not sem.acquire(timeout=getattr(self.server, "queue_wait_s", None) or 60.0):
            return b'{"error": "busy"}\n', "bad_request", "busy"
        try:
            label = "bad_request"       # review B3: the log names only a RECOGNIZED op, never caller bytes
            try:
                try:
                    req = json.loads(line)
                except ValueError:
                    req = None                  # unparseable: answered bad_request below, connection kept
                del line                        # this frame's reference; the caller drops its own before waiting
                is_obj = isinstance(req, dict)
                op = req.get("op") if is_obj else None
                args = req.get("args") if is_obj else None
                req = None
                if not isinstance(op, str) or op not in self.server.ops:
                    label = "unknown_op" if is_obj else "bad_request"
                    rep = {"error": label}
                elif args is not None and not isinstance(args, dict):
                    label, rep = op, {"error": "bad_request"}
                else:
                    label = op
                    self.store = self.store or self.server.make_store()
                    rep = self.server.dispatch(self.store, op, args or {})
                args = None
            except Exception as e:  # noqa: BLE001  (a failed request never kills the connection's peer handling)
                rep = {"error": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None)}
            outcome = "ok" if "ok" in rep else rep.get("error")
            return (json.dumps(rep) + "\n").encode(), label, outcome
        finally:
            if sem is not None:
                sem.release()

    def handle(self):
        uid = self.client_address
        buf = bytearray()
        try:
            while True:
                try:
                    line, buf = self._line(buf)
                except (_TooLong, _Slow) as e:
                    err = "request_too_large" if isinstance(e, _TooLong) else "request_timeout"
                    self.server.note(f"uid={uid} op=bad_request {err}")
                    self._reply({"error": err})
                    return
                except _Idle:
                    self.server.note(f"uid={uid} idle_timeout")
                    return
                if line is None:
                    return
                data, label, outcome = self._serve(line)
                line = None
                self.server.note(f"uid={uid} op={label} {outcome}")
                self._send(data)
                data = None
        except OSError:
            return                          # peer gone, or a reply not taken within the deadline: connection dropped
        finally:
            ks = self.store
            if ks is not None:
                if hasattr(ks, "close"):
                    ks.close()                  # the seal authority: its store connection and its key-service client
                elif getattr(ks, "conn", None) is not None:
                    ks.conn.close()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Transport bounds (review J N2), set per service and overridable per instance:
      line_limit          bytes of one request line, newline included
      request_timeout_s   from a request's first byte to its newline; also the send of one reply
      max_per_uid         live connections of one peer uid
      max_conn            live connections in all
      idle_timeout_s      longest wait for a request's first byte (None: no bound; review 42cfd11 R2)
      work_slots          requests parsed, dispatched and encoded at once (None: no bound; review 42cfd11 R2a)
      queue_wait_s        longest wait for a work slot, then busy (default 60 s)
    A peer over a connection cap gets {"error": "busy"} and is closed in the accept loop, before a thread is started; a
    refused uid likewise gets peer_refused. Raw bytes held: at most max_conn x line_limit in requests being read plus,
    per connection, one encoded reply being sent. Parsed requests: one per work slot (review 42cfd11 R2a: a JSON line
    of empty objects costs about 28 times its size once parsed, so the slots, not the line limit, bound that)."""
    daemon_threads = True

    def __init__(self, path, allow, make_store=None, uid_of=peer_uid, log=None, ops=OPS, dispatch_fn=None,
                 line_limit=LINE_LIMIT, request_timeout_s=REQUEST_TIMEOUT_S, max_per_uid=MAX_CONN_PER_UID, max_conn=MAX_CONN,
                 idle_timeout_s=None, work_slots=None, queue_wait_s=60.0):
        """ops / dispatch_fn: the service's operations (default: the key service's); the broker (wp/broker.py) reuses
        this transport and its kernel-uid check with its own."""
        if os.path.exists(path):
            os.unlink(path)
        old = os.umask(0o111)          # mode 0666 at bind (reachability only: the kernel-reported uid decides who is
        try:                           # served); no chmod by path after bind, which would follow a symlink (review J M13)
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        self.allow, self.uid_of, self.log = frozenset(allow), uid_of, log
        self.make_store = make_store or K.KeyService
        self.ops, self.dispatch = frozenset(ops), dispatch_fn or dispatch
        self.line_limit, self.request_timeout_s = int(line_limit), float(request_timeout_s)
        self.max_per_uid, self.max_conn = int(max_per_uid), int(max_conn)
        self.idle_timeout_s = None if idle_timeout_s is None else float(idle_timeout_s)
        self.work_slots = None if work_slots is None else int(work_slots)
        self.queue_wait_s = float(queue_wait_s)
        self._lock, self._live, self._per_uid = threading.Lock(), 0, {}
        self._work = None

    def work_semaphore(self):
        """The work slots as a semaphore, made on first use (so a bound set on the instance after construction holds)."""
        if not self.work_slots:
            return None
        with self._lock:
            if self._work is None:
                self._work = threading.BoundedSemaphore(self.work_slots)
            return self._work

    def process_request(self, request, client_address):
        try:
            uid = self.uid_of(request)
        except OSError:
            self.shutdown_request(request)
            return
        with self._lock:
            if uid not in self.allow:
                err = "peer_refused"
            elif self._live >= self.max_conn or self._per_uid.get(uid, 0) >= self.max_per_uid:
                err = "busy"
            else:
                err = None
                self._live += 1
                self._per_uid[uid] = self._per_uid.get(uid, 0) + 1
        if err:
            _send_nowait(request, ('{"error": "%s"}\n' % err).encode())
            self.note(f"refused peer uid={uid}" if err == "peer_refused" else f"busy uid={uid}")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, uid)      # the handler sees the admitted uid as client_address
        except BaseException:
            self._release(uid)
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, uid):
        try:
            super().process_request_thread(request, uid)
        finally:
            self._release(uid)

    def _release(self, uid):
        with self._lock:
            self._live -= 1
            self._per_uid[uid] -= 1
            if not self._per_uid[uid]:
                del self._per_uid[uid]

    def note(self, s):
        if self.log:
            self.log(s)


class ServiceError(Exception):
    pass


def exchange(f, data):
    """Write one request line to the socket file `f` and read one reply line (b"" when the peer closed). A server that
    refused the connection in its accept loop (peer_refused, busy) may have closed it before the write: the write's
    broken pipe is ignored and its one line is still read (review J N2)."""
    try:
        f.write(data)
        f.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return f.readline()


def close_quietly(f, sock):
    try:
        f.close()
    except OSError:
        pass                      # a request left unsent in the buffer of a refused connection
    finally:
        sock.close()


def _parts(items, budget=CLIENT_LINE):
    """Consecutive slices of `items` whose JSON encoding stays under `budget` bytes (one request each; order kept).
    An empty list is one empty slice."""
    part, size = [], 0
    for x in items:
        n = len(json.dumps(x)) + 2
        if part and size + n > budget:
            yield part
            part, size = [], 0
        part.append(x)
        size += n
    yield part


class Client(K.KeyStore):
    """KeyStore over the key service's socket. Same methods and return types as wp.keys.KeyService."""

    def __init__(self, path=None, timeout_s=None):
        """timeout_s (default CLIENT_TIMEOUT_S; review J M11): the longest wait for one reply. A timed-out connection
        is closed and every later call raises ServiceError (a late reply would answer the wrong request)."""
        self.path = path or os.environ["WP_KEYSVC"]
        self.timeout_s = CLIENT_TIMEOUT_S if timeout_s is None else float(timeout_s)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout_s)
        self.sock.connect(self.path)
        self.f = self.sock.makefile("rwb")
        self._desc = None
        self._broken = False

    def _call(self, op, _wait_s=0.0, **a):
        data = (json.dumps({"op": op, "args": a}) + "\n").encode()
        if len(data) > LINE_LIMIT:
            raise ServiceError("request_too_large")
        if self._broken:
            raise ServiceError("key service connection unusable after a timeout")
        try:
            self.sock.settimeout(self.timeout_s + _wait_s)
            line = exchange(self.f, data)
        except socket.timeout:
            self._broken = True
            self.close()
            raise ServiceError("key service timed out") from None
        if not line:
            raise ServiceError("key service closed the connection")
        rep = json.loads(line)
        if "ok" in rep:
            return rep["ok"]
        if rep.get("error") == "SubjectErased":
            raise K.SubjectErased(rep.get("subject"))
        raise ServiceError(rep.get("error") or "unknown")

    def keys(self, tenant, subjects, create=False):
        got = {}
        for part in _parts(sorted(set(subjects))):
            got.update(self._call("keys", tenant=tenant, subjects=part, create=create))
        return {s: (bytes.fromhex(v) if v is not None else None) for s, v in got.items()}

    def tenant_hmac(self, tenant, text):
        return self._call("tenant_hmac", tenant=tenant, text=text)

    def tenant_hmac_many(self, tenant, texts):
        out = []
        for part in _parts(list(texts)):
            out.extend(self._call("tenant_hmac_many", tenant=tenant, texts=part))
        return out

    def chain_key(self, tenant):
        return bytes.fromhex(self._call("chain_key", tenant=tenant))

    def is_destroyed(self, tenant, subject):
        return self._call("is_destroyed", tenant=tenant, subject=subject)

    def key_exists(self, tenant, subject):
        return self._call("key_exists", tenant=tenant, subject=subject)

    def destroy(self, tenant, subject):
        self._call("destroy", tenant=tenant, subject=subject)

    def purge(self, timeout_s=60.0, poll_s=0.25):
        return self._call("purge", _wait_s=float(timeout_s), timeout_s=timeout_s)

    def physical_pending(self, tenant=None):
        return [tuple(r) for r in self._call("physical_pending", tenant=tenant)]

    def describe(self):
        if self._desc is None:
            self._desc = dict(self._call("describe"), transport="separate key-service process over a unix socket; "
                              "peer identified by kernel uid (SO_PEERCRED); the application receives subject key bytes "
                              "(app-side encryption)")
        return self._desc

    def close(self):
        close_quietly(self.f, self.sock)


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
