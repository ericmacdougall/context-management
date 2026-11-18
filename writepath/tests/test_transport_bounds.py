"""Offline unit test (no stores): the transport shared by the key service, the broker and the seal authority
(wp/keysvc.py Server) bounds what one peer can make it hold, and its clients do not wait forever (review J N2, M11, M13;
built from probe P1, which streamed 3 GiB without a newline into the authority's handler and hit 1 GB RSS in 1.2 s).
  TB1  P1: a line with no newline is cut at the server's line limit: one request_too_large reply, the connection is
       closed, the server holds at most the limit (peak RSS of this process grows by under 32 MiB while 64 MiB are
       offered); the caller's bytes never reach the log
  TB2  the boundary: a request line of exactly line_limit bytes (newline included) is served, one byte more is refused
  TB3  a request started and not finished within request_timeout_s gets request_timeout and is closed; an IDLE
       connection between requests is not (it is served after waiting longer than the timeout)
  TB4  per-uid cap: the third connection of a uid capped at 2 gets busy before sending anything; a slot freed by a
       close is reusable
  TB5  overall cap: with 3 in all, a fourth connection of a uid below its own cap gets busy
  TB6  the seal authority's limit admits the largest admitted call at worst-case JSON escaping (call_bytes of control
       characters in call_events events); serve() wires the authority's own bounds into the transport
  TB7  M11: the key-service and seal-authority clients give up on a peer that accepts and never answers
       (ServiceError, SealUnavailable) instead of blocking the appender
  TB8  M13: the server never chmods the socket by path after binding (a path chmod follows a swapped-in symlink)
Bounds are set on the server instance after construction, so the same test runs unchanged against a tree without them
(red by assertion, not by a TypeError). Needs WP_UCD_DIR for TB6 and TB7's sealer half (the matcher fails closed).
usage (from writepath/): WP_UCD_DIR=<dir> python3 -m unittest tests.test_transport_bounds -v"""
import contextlib
import importlib
import io
import json
import os
import pathlib
import resource
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parents[1]
UID_A, UID_B, UID_C = 20001, 20002, 20003
MARK = "zqtransportcallerbytes"
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
_SET_HOME = []


def _stub(name, **attrs):
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


def _imports():
    if not os.environ.get("WP_HOME"):
        home = pathlib.Path(tempfile.mkdtemp()) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        _SET_HOME.append(1)
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _stub("psycopg", connect=lambda *a, **k: None, Error=Exception, errors=types.SimpleNamespace(UniqueViolation=Exception))
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        for n in ("cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives", "cryptography.hazmat.primitives.ciphers"):
            _stub(n)
        _stub("cryptography.hazmat.primitives.ciphers.aead", AESGCM=object)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return importlib.import_module("wp.keysvc")


def tearDownModule():
    if _SET_HOME:
        os.environ.pop("WP_HOME", None)


def echo(ks, op, a):
    return {"ok": len(str(a.get("x", "")))}


class Transport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.S = _imports()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()

    def server(self, allow=(UID_A,), uid_of=None, **bounds):
        path = os.path.join(self.tmp, "t%d.sock" % len(self.servers))
        log = []
        srv = self.S.Server(path, set(allow), make_store=lambda: types.SimpleNamespace(conn=None),
                            uid_of=uid_of or (lambda s: UID_A), log=log.append, ops=("echo",), dispatch_fn=echo)
        for k, v in bounds.items():
            setattr(srv, k, v)            # a tree without the bounds ignores these: red by assertion below
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return path, log

    def conn(self, path, timeout=5.0):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        c.connect(path)
        return c

    def lines(self, c, wait=5.0):
        """Every line the server sends until it closes or `wait` passes: (lines, closed)."""
        c.settimeout(wait)
        buf, closed = b"", False
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                got = c.recv(65536)
            except socket.timeout:
                break
            except OSError:
                closed = True
                break
            if not got:
                closed = True
                break
            buf += got
        return [json.loads(x) for x in buf.split(b"\n") if x.strip()], closed

    def roundtrip(self, c, x="a"):
        c.sendall((json.dumps({"op": "echo", "args": {"x": x}}) + "\n").encode())
        f = b""
        while not f.endswith(b"\n"):
            got = c.recv(65536)
            if not got:
                return None
            f += got
        return json.loads(f)

    # ------------------------------------------------------------------ TB1: probe P1
    def test_tb1_unterminated_line_is_cut_at_the_limit(self):
        lim = 64 * 1024
        path, log = self.server(line_limit=lim, request_timeout_s=30.0)
        c = self.conn(path)
        body = (MARK * (1 << 16))[: 1 << 20].encode()
        sent = [0]

        def pump():
            try:
                c.sendall(b'{"op": "echo", "args": {"x": "')
                while sent[0] < 64:
                    c.sendall(body)
                    sent[0] += 1
            except OSError:
                pass
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        th.join(30)
        got, closed = self.lines(c, wait=5.0)
        grew_mib = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / (1024 * 1024 if sys.platform == "darwin" else 1024)
        c.close()
        self.assertEqual(got, [{"error": "request_too_large"}], f"no refusal after {sent[0]} MiB offered")
        self.assertTrue(closed, "the connection stayed open after an over-limit line")
        self.assertLess(sent[0], 64, "the server read all 64 MiB of an unterminated line")
        self.assertLess(grew_mib, 32, f"peak RSS grew {grew_mib:.0f} MiB")
        self.assertNotIn(MARK, "\n".join(log), "caller bytes reached the log")

    # ------------------------------------------------------------------ TB2: the exact boundary
    def test_tb2_line_limit_boundary(self):
        lim = 4096
        path, _ = self.server(line_limit=lim)

        head, tail = '{"op": "echo", "args": {"x": "', '"}}\n'

        def line(n):
            return (head + "a" * (n - len(head) - len(tail)) + tail).encode()
        self.assertEqual(len(line(lim)), lim)
        c = self.conn(path)
        c.sendall(line(lim))
        got, _ = self.lines(c, wait=2.0)
        c.close()
        self.assertEqual(got, [{"ok": lim - len(head) - len(tail)}], "a line of exactly line_limit bytes was not served")
        c = self.conn(path)
        c.sendall(line(lim + 1))
        got, closed = self.lines(c, wait=3.0)
        c.close()
        self.assertEqual(got, [{"error": "request_too_large"}], "a line one byte over the limit was served")
        self.assertTrue(closed)

    # ------------------------------------------------------------------ TB3: request deadline, idle is free
    def test_tb3_slow_request_times_out_idle_does_not(self):
        path, _ = self.server(request_timeout_s=1.0)
        c = self.conn(path)
        c.sendall(b'{"op": "ec')
        t0 = time.monotonic()
        got, closed = self.lines(c, wait=6.0)
        c.close()
        self.assertEqual(got, [{"error": "request_timeout"}], "a request left unfinished was never cut")
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - t0, 5.0)
        c = self.conn(path)
        time.sleep(1.6)
        self.assertEqual(self.roundtrip(c), {"ok": 1}, "an idle connection was dropped")
        c.close()

    # ------------------------------------------------------------------ TB4: per-uid cap
    def test_tb4_per_uid_cap(self):
        path, log = self.server(max_per_uid=2, max_conn=10)
        c1, c2 = self.conn(path), self.conn(path)
        self.assertEqual(self.roundtrip(c1), {"ok": 1})
        self.assertEqual(self.roundtrip(c2), {"ok": 1})
        c3 = self.conn(path)
        got, closed = self.lines(c3, wait=3.0)
        c3.close()
        self.assertEqual(got, [{"error": "busy"}], "a third connection of a uid capped at 2 was taken")
        self.assertTrue(closed)
        c1.close()
        ok = None
        end = time.monotonic() + 5.0
        while time.monotonic() < end and ok != {"ok": 1}:
            c4 = self.conn(path)
            try:
                ok = self.roundtrip(c4)
            except OSError:
                ok = None
            c4.close()
            time.sleep(0.05)
        self.assertEqual(ok, {"ok": 1}, "a slot freed by a close was not reusable")
        c2.close()

    # ------------------------------------------------------------------ TB5: overall cap
    def test_tb5_overall_cap(self):
        order = iter([UID_A, UID_A, UID_B, UID_B, UID_C])
        path, _ = self.server(allow=(UID_A, UID_B, UID_C), uid_of=lambda s: next(order), max_per_uid=2, max_conn=3)
        cs = [self.conn(path) for _ in range(3)]
        for c in cs:
            self.assertEqual(self.roundtrip(c), {"ok": 1})
        c4 = self.conn(path)
        got, closed = self.lines(c4, wait=3.0)
        c4.close()
        self.assertEqual(got, [{"error": "busy"}], "a fourth connection was taken with 3 allowed in all")
        for c in cs:
            c.close()

    # ------------------------------------------------------------------ TB6: the authority's limit and wiring
    def sealer(self):
        if not os.environ.get("WP_UCD_DIR"):
            self.skipTest("WP_UCD_DIR not set (the matcher fails closed without its tables)")
        return importlib.import_module("wp.sealer")

    def test_tb6_authority_limit_admits_the_largest_call(self):
        SL = self.sealer()
        M = importlib.import_module("wp.mentions")
        n, per = M.BUDGET.call_events, M.BUDGET.call_bytes // M.BUDGET.call_events
        req = {"op": "seal", "args": {"tenant": "t" * 64, "lease_scope": "session:" + "s" * 64, "lease_epoch": 2**31,
                                      "batch_id": uuid.uuid4().hex, "purpose": "quarantine",
                                      "events": [{"event_id": uuid.uuid4().hex, "subject_id": "S" * 64, "content": "\x01" * per}
                                                 for _ in range(n)]}}
        size = len((json.dumps(req) + "\n").encode())
        lim = getattr(SL, "LINE_LIMIT", None)
        self.assertIsNotNone(lim, "the seal authority states no request line limit")
        self.assertLessEqual(size, lim, f"the largest admitted call ({size} bytes) is over the authority's limit")

    def test_tb6b_serve_wires_the_authority_bounds(self):
        SL = self.sealer()
        seen = {}

        class Rec:
            def __init__(self, path, allow, **kw):
                seen.update(kw)

            def serve_forever(self):
                return None
        d = tempfile.mkdtemp()
        SL.new_key_file(os.path.join(d, "seal_key"))
        env = {k: os.environ.get(k) for k in ("WP_SECRETS_DIR", "WP_SEALER_ALLOW_UIDS")}
        real = self.S.Server
        try:
            os.environ["WP_SECRETS_DIR"], os.environ["WP_SEALER_ALLOW_UIDS"] = d, str(UID_A)
            self.S.Server = Rec
            with contextlib.redirect_stdout(io.StringIO()):      # serve() prints the (throwaway) key id
                SL.serve(os.path.join(d, "s.sock"))
        finally:
            self.S.Server = real
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIsNotNone(seen.get("line_limit"), "serve() did not set the line limit")
        self.assertEqual(seen.get("line_limit"), getattr(SL, "LINE_LIMIT", None), "serve() set another line limit")
        for k in ("request_timeout_s", "max_per_uid", "max_conn"):
            self.assertIsNotNone(seen.get(k), f"serve() did not set {k}")

    # ------------------------------------------------------------------ TB7: client timeouts (M11)
    def silent_peer(self):
        path = os.path.join(self.tmp, "silent.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(4)                        # accepts by backlog, never reads or answers
        self.addCleanup(s.close)
        return path

    def gives_up(self, mod, call, exc):
        old = getattr(mod, "CLIENT_TIMEOUT_S", None)
        mod.CLIENT_TIMEOUT_S = 1.0         # a tree without client timeouts ignores this
        out = {}

        def run():
            try:
                call()
                out["r"] = "returned"
            except Exception as e:  # noqa: BLE001
                out["r"] = e
        try:
            th = threading.Thread(target=run, daemon=True)
            t0 = time.monotonic()
            th.start()
            th.join(8.0)
        finally:
            if old is None:
                del mod.CLIENT_TIMEOUT_S
            else:
                mod.CLIENT_TIMEOUT_S = old
        self.assertFalse(th.is_alive(), "the client is still waiting on a peer that never answers")
        self.assertIsInstance(out.get("r"), exc)
        self.assertLess(time.monotonic() - t0, 8.0)

    def test_tb7_keysvc_client_times_out(self):
        path = self.silent_peer()
        self.gives_up(self.S, lambda: self.S.Client(path).chain_key("t"), self.S.ServiceError)

    def test_tb7b_sealer_client_times_out(self):
        SL = self.sealer()
        path = self.silent_peer()
        self.gives_up(SL, lambda: SL.Client(path).seal("t", "session:s", 1, "b", [{"event_id": "e", "subject_id": "X", "content": "hi"}]),
                      SL.SealUnavailable)

    # ------------------------------------------------------------------ TB8: no chmod by path (M13)
    def test_tb8_no_chmod_by_path(self):
        calls = []
        real = self.S.os.chmod
        self.S.os.chmod = lambda p, m, *a, **k: calls.append((str(p), m))
        try:
            path, _ = self.server()
        finally:
            self.S.os.chmod = real
        self.assertEqual(calls, [], "the server chmods the socket by path after binding")
        if sys.platform.startswith("linux"):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o666, "the socket is not mode 0666")


if __name__ == "__main__":
    unittest.main()
