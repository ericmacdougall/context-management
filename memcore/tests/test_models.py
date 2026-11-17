import json
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memcore.adapters.elastic_models import ElasticInferenceEmbedder, ElasticInferenceReranker
from memcore.budget import CallBudget
from memcore.fakes import HashEmbedder


class Es:
    def __init__(self):
        self.reqs = []
        es = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                es.reqs.append((self.path, b))
                if "/text_embedding/" in self.path:
                    out = {"text_embedding": [{"embedding": [1.0, 0.0, 0.0]} for _ in b["input"]]}
                else:
                    out = {"rerank": [{"index": i, "relevance_score": float(len(d))} for i, d in enumerate(b["input"])]}
                raw = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def check_prefix_sensitivity(with_prefix, without_prefix, threshold=0.999):
    """A contract test the retrieval owner can reuse: vectors made with and without the E5 prefix must differ measurably;
    if an adapter silently drops the prefix, the 'with' and 'without' vectors coincide and this fails."""
    return min(cos(a, b) for a, b in zip(with_prefix, without_prefix)) < threshold


class ModelTests(unittest.TestCase):
    def test_embedder_always_sends_input_type_and_port_adds_no_prefix(self):
        es = Es()
        e = ElasticInferenceEmbedder(es.url, "mc-e5", lambda: "ApiKey t", 3, budget=CallBudget(tempfile.mkdtemp(), 10))
        e.embed(["a"], "query"); e.embed(["a"], "passage")
        self.assertEqual([b["input_type"] for _, b in es.reqs], ["search", "ingest"])
        self.assertEqual([b["input"] for _, b in es.reqs], [["a"], ["a"]])  # no double prefix
        es.srv.shutdown()

    def test_reranker_orders_by_score(self):
        es = Es()
        r = ElasticInferenceReranker(es.url, "mc-rerank", lambda: "ApiKey t", budget=CallBudget(tempfile.mkdtemp(), 10)).rerank("q", ["aa", "bbbb", "c"], 2)
        self.assertEqual([h.index for h in r], [1, 0, 2])
        self.assertEqual(es.reqs[0][1]["top_n"], 2)
        es.srv.shutdown()

    def test_prefix_omission_is_detected_and_broken_variant_passes_silently(self):
        texts = ["staging database dublin", "refund policy"]
        good, raw = HashEmbedder(), HashEmbedder()
        raw.query_prefix = ""
        self.assertTrue(check_prefix_sensitivity(good.embed(texts, "query").vectors, raw.embed(texts, "query").vectors))
        broken = HashEmbedder(); broken.query_prefix = ""
        self.assertFalse(check_prefix_sensitivity(broken.embed(texts, "query").vectors, raw.embed(texts, "query").vectors))  # dropped prefix: detected as equal


if __name__ == "__main__":
    unittest.main()
