import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memcore.adapters.elasticsearch_search import CrossTenantError, ElasticsearchSearch


class Node:
    """Stub node: records requests; answers _search with fixed hits; a retriever query gets 403 when licence_ok is False."""
    def __init__(self, licence_ok=True):
        self.reqs, self.licence_ok, self.fail_once = [], licence_ok, None
        node = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                node.reqs.append((self.path, body))
                if node.fail_once and "retriever" in body:
                    code, out = node.fail_once, {"error": {"reason": "service unavailable"}}
                    node.fail_once = None
                elif "retriever" in body and not node.licence_ok:
                    out, code = {"error": {"reason": "current license is non-compliant for [Reciprocal Rank Fusion (RRF)]"}}, 403
                else:
                    code, out = 200, {"hits": {"hits": [{"_id": "a", "_score": 2.0, "_source": {"text": "x"}}, {"_id": "b", "_score": 1.0, "_source": {}}]}}
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()


def mk(node, **kw):
    return ElasticsearchSearch(node.url, lambda: "ApiKey t", "acme", **kw)


class EsSearchTests(unittest.TestCase):
    def test_request_shapes(self):
        n = Node()
        try:
            s = mk(n, rescore_oversample=3.0)
            s.lexical("docs", "hello", {"group": "g1"}, 5)
            s.vector("docs", [0.1, 0.2], {"group": "g1"}, 5)
            (p1, b1), (p2, b2) = n.reqs
            self.assertEqual(p1, "/mc-acme-docs/_search")
            self.assertEqual(b1["query"]["bool"]["filter"], [{"term": {"tenant": "acme"}}, {"term": {"group": "g1"}}])
            self.assertEqual(b2["knn"]["filter"], [{"term": {"tenant": "acme"}}, {"term": {"group": "g1"}}])
            self.assertEqual((b2["knn"]["k"], b2["knn"]["num_candidates"], b2["knn"]["rescore_vector"]), (5, 100, {"oversample": 3.0}))
        finally:
            n.stop()

    def test_cross_tenant_and_bad_names_refused_before_any_request(self):
        n = Node()
        try:
            s = mk(n)
            with self.assertRaises(CrossTenantError):
                s.lexical("docs", "x", tenant="other")
            for bad in ("*", "a,b", "../x", "_all", "Docs"):
                with self.assertRaises(ValueError):
                    s.lexical(bad, "x")
            self.assertEqual(n.reqs, [])
        finally:
            n.stop()

    def test_auto_uses_backend_when_licensed(self):
        n = Node()
        try:
            r = mk(n).fused("docs", "q", [0.0, 1.0], None, 2)
            self.assertEqual(r.fusion, "backend")
            self.assertIn("rrf", n.reqs[-1][1]["retriever"])
        finally:
            n.stop()

    def test_auto_falls_back_to_client_on_licence_error_and_reports_it(self):
        n = Node(licence_ok=False)
        try:
            s = mk(n)
            r = s.fused("docs", "q", [0.0, 1.0], None, 2)
            self.assertEqual((r.fusion, s.fusion_location), ("client", "client"))
            self.assertIn("non-compliant", s.probe_error)
            self.assertEqual([x.id for x in r.hits], ["a", "b"])
            kinds = [("retriever" in b, "knn" in b) for _, b in n.reqs]
            self.assertEqual(kinds, [(True, False), (False, False), (False, True)])  # probe, lexical, vector
        finally:
            n.stop()

    def test_explicit_backend_does_not_swallow_licence_error(self):
        n = Node(licence_ok=False)
        try:
            from memcore.ports import BackendError
            with self.assertRaises(BackendError):
                mk(n, strategy="backend").fused("docs", "q", [0.0], None, 2)
        finally:
            n.stop()

    def test_linear_retriever_body(self):
        n = Node()
        try:
            mk(n, backend_fusion="linear", strategy="backend").fused("docs", "q", [0.0], None, 2)
            self.assertIn("linear", n.reqs[-1][1]["retriever"])
        finally:
            n.stop()


class ReviewTenantTests(unittest.TestCase):
    def test_item1_10_no_ambiguous_physical_names(self):
        a = ElasticsearchSearch("http://127.0.0.1:1", lambda: "k", tenant="acme")
        with self.assertRaises(ValueError):
            ElasticsearchSearch("http://127.0.0.1:1", lambda: "k", tenant="acme-eu")      # '-' is the separator: refused
        b = ElasticsearchSearch("http://127.0.0.1:1", lambda: "k", tenant="acme_eu")
        self.assertNotEqual(a.index_name("eu-docs"), b.index_name("docs"))              # the old collision is gone
        for bad in ("docs\n", "docs.", "../x", "", "a b", "Docs"):
            with self.assertRaises(ValueError):
                a.index_name(bad)
        for bad in ("acme\n", "acme.", "ac-me"):
            with self.assertRaises(ValueError):
                ElasticsearchSearch("http://127.0.0.1:1", lambda: "k", tenant=bad)

    def test_item10_create_bulk_delete_take_tenant_check_and_stamp_documents(self):
        n = Node()
        try:
            s = mk(n)
            for call in (lambda: s.create_index("docs", 2, tenant="other"), lambda: s.delete_index("docs", tenant="other"),
                         lambda: s.bulk_add("docs", {"a": {"text": "x"}}, tenant="other"),
                         lambda: s.bulk_add("docs", {"a": {"text": "x", "tenant": "other"}})):
                with self.assertRaises(CrossTenantError):
                    call()
            self.assertEqual(n.reqs, [])
            with self.assertRaises(CrossTenantError):
                s.lexical("docs", "x", {"tenant": "other"})                             # caller cannot override the tenant filter
        finally:
            n.stop()


class ReviewProbeTests(unittest.TestCase):
    def test_item11_transient_error_is_not_latched(self):
        n = Node()
        try:
            s = mk(n)
            n.fail_once = 503
            r1 = s.fused("docs", "q", [0.0], None, 2)          # probe hits a 503: client for this call only
            self.assertEqual((r1.fusion, s._probe), ("client", None))
            r2 = s.fused("docs", "q", [0.0], None, 2)          # next call re-probes and uses the backend
            self.assertEqual(r2.fusion, "backend")
        finally:
            n.stop()

    def test_item11_licence_lapse_midrun_falls_back_then_reprobes(self):
        n = Node()
        try:
            s = mk(n)
            self.assertEqual(s.fused("docs", "q", [0.0], None, 2).fusion, "backend")
            n.licence_ok = False
            self.assertEqual(s.fused("docs", "q", [0.0], None, 2).fusion, "client")   # no 403 reaches the caller
            n.licence_ok = True
            s._probe_at -= 1000                                                       # TTL passed
            self.assertEqual(s.fused("docs", "q", [0.0], None, 2).fusion, "backend")
        finally:
            n.stop()

    def test_item11_plain_403_is_not_a_licence_error(self):
        n = Node()
        try:
            s = mk(n)
            n.fail_once = 403
            r = s.fused("docs", "q", [0.0], None, 2)
            self.assertEqual((r.fusion, s._probe), ("client", None))                  # not latched as 'no licence'
        finally:
            n.stop()


if __name__ == "__main__":
    unittest.main()
