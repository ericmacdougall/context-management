"""Deploy part 4 (goodqa#16096): the adapters' HTTP helper must not reload the system CA store on every HTTPS call.

Method: count ssl.SSLContext.load_default_certs calls while http_util.urlopen is driven against a CLOSED loopback port. A
default TLS context is created when the HTTPS connection object is built, which is before the connect is refused, so every
call reaches the code under test and no network or certificate is needed. The witness is that every call really ran and
failed with a connection error, so a zero count cannot come from calls that never got that far. On the prior implementation
(an opener built per call) the count equals the number of calls; the fix makes it at most 1."""
import socket
import ssl
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memcore import http_util


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CountLoads:
    """Counts CA-store loads; restores the originals."""

    def __enter__(self):
        self.n = 0
        self._orig = ssl.SSLContext.load_default_certs
        outer = self

        def counting(ctx, *a, **k):
            outer.n += 1
            return self._orig(ctx, *a, **k)
        ssl.SSLContext.load_default_certs = counting
        return self

    def __exit__(self, *exc):
        ssl.SSLContext.load_default_certs = self._orig


def _refused_calls(n, context=None, port=None):
    port = port or _closed_port()
    failed = 0
    for _ in range(n):
        try:
            http_util.urlopen(urllib.request.Request("https://127.0.0.1:%d/x" % port), timeout=2, context=context)
        except (urllib.error.URLError, OSError):
            failed += 1
    return failed


class CertStoreLoadedOnce(unittest.TestCase):
    def setUp(self):
        # reset module caches so the count is independent of test order (works on the prior module too: attributes absent)
        for name, val in (("_default_ctx", None),):
            if hasattr(http_util, name):
                setattr(http_util, name, val)
        if hasattr(http_util, "_openers"):
            http_util._openers.clear()

    def test_default_context_calls_load_the_ca_store_once(self):
        with _CountLoads() as c:
            failed = _refused_calls(20)
        self.assertEqual(failed, 20, "witness: all 20 calls ran and were refused at connect")
        self.assertLessEqual(c.n, 1, "CA store loaded %d times for 20 calls" % c.n)

    def test_concurrent_calls_share_one_load(self):
        port = _closed_port()
        res = []

        def work():
            res.append(_refused_calls(5, port=port))
        with _CountLoads() as c:
            ts = [threading.Thread(target=work) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(sum(res), 40, "witness: 40 calls ran and were refused")
        self.assertLessEqual(c.n, 1, "CA store loaded %d times for 40 concurrent calls" % c.n)

    def test_a_supplied_context_is_used_as_given_and_not_shared(self):
        verified = ssl.create_default_context()
        lax = ssl.create_default_context()
        lax.check_hostname, lax.verify_mode = False, ssl.CERT_NONE
        o1, o2 = http_util._opener_for(verified), http_util._opener_for(lax)
        self.assertIsNot(o1, o2)
        self.assertIs(http_util._opener_for(verified), o1, "same context, same opener")
        ctxs = [h._context for h in o2.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(ctxs, [lax], "the opener for the unverified context carries exactly that context")
        ctxs = [h._context for h in o1.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(ctxs, [verified])
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED, "the verifying context stayed verifying")

    def test_default_context_verifies(self):
        ctx = http_util.default_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_cache_is_bounded(self):
        for _ in range(http_util._MAX_CACHED + 10):
            http_util._opener_for(ssl.create_default_context())
        self.assertLessEqual(len(http_util._openers), http_util._MAX_CACHED)


class RedirectStillRefusedThroughTheCachedOpener(unittest.TestCase):
    def test_a_302_is_an_http_error_and_the_target_is_never_called(self):
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for _ in range(2):      # twice: the second call goes through the cached opener
                req = urllib.request.Request("http://127.0.0.1:%d/start" % srv.server_address[1], headers={"Authorization": "Bearer x"})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    http_util.urlopen(req, timeout=2)
                self.assertEqual(cm.exception.code, 302)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual([p for p, _ in seen], ["/start", "/start"], "the redirect target was never requested")


if __name__ == "__main__":
    unittest.main()
