"""Golden fixtures recorded from the live Elasticsearch 9.5.6 trial node (see NOTES.md, G5): replayed with no model."""
import base64
import io
import json
import os
import struct
import tempfile
import unittest
import urllib.request

from memcore.adapters.elastic_inference import ElasticInferenceChat
from memcore.fakes import FakeChat, HashEmbedder
from memcore.gateway import Gateway
from memcore.sse import parse_events

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def adapter():
    d = tempfile.mkdtemp()
    return ElasticInferenceChat("http://127.0.0.1:1", "mc-x", lambda: "ApiKey test", cache_dir=d, ledger_path=os.devnull, cap=5)


class GoldenStreams(unittest.TestCase):
    def test_real_bytes_have_bom_and_bare_chunks(self):
        raw = open(os.path.join(FX, "es956_stream_plain.sse"), "rb").read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), raw.count(b"event: message"))   # a BOM before every event
        self.assertNotIn(b'"chat_completion"', raw)                                  # no wrapper key on 9.5.6

    def test_adapter_parses_real_plain_stream(self):
        out = adapter().read_stream(io.BytesIO(open(os.path.join(FX, "es956_stream_plain.sse"), "rb").read()))
        self.assertIn("paris", out.text.lower())
        self.assertIsNotNone(out.usage.output_tokens)

    def test_adapter_parses_real_forced_tool_stream(self):
        out = adapter().read_stream(io.BytesIO(open(os.path.join(FX, "es956_stream_tools.sse"), "rb").read()))
        self.assertEqual(json.loads(out.text)["city"], "Lisbon")
        self.assertTrue(out.meta["forced_tool"])

    def test_broken_variant_naive_parser_chokes_on_bom(self):
        raw = open(os.path.join(FX, "es956_stream_plain.sse"), "rb").read()
        names = [l.decode().split(":")[0] for l in raw.splitlines() if l.strip()]
        self.assertIn("﻿event", names)  # a parser that does not strip the BOM sees 'event' as an unknown field name


class RecordedRequests(unittest.TestCase):
    def test_gateway_answers_the_recorded_validation_and_embedding_requests(self):
        rq = json.load(open(os.path.join(FX, "es956_requests.json")))
        d = tempfile.mkdtemp()
        b = FakeChat(lambda p, s, j: "fine", cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=9)
        b.model = "stub-chat"
        gw = Gateway({"stub-chat": b}, os.path.join(d, "g"), embedder=HashEmbedder(64))
        gw.start()
        try:
            tok = open(os.path.join(d, "g", "gateway.token")).read().strip()

            def post(path, body):
                return urllib.request.urlopen(urllib.request.Request(gw.base_url + path, data=json.dumps(body).encode(), method="POST",
                                                                     headers={"Authorization": "Bearer " + tok}))
            raw = post("/chat/completions", rq["chat_validation_request"]["body"]).read()
            evs = list(parse_events(raw.splitlines(True)))
            self.assertEqual(evs[-1], "[DONE]")
            self.assertEqual(sorted(evs[-2]["usage"]), ["completion_tokens", "prompt_tokens", "prompt_tokens_details", "total_tokens"])
            e = json.load(post("/embeddings", rq["embedding_request"]["body"]))
            v = struct.unpack("<64f", base64.b64decode(e["data"][0]["embedding"]))
            self.assertEqual(len(v), 64)
        finally:
            gw.shutdown()


if __name__ == "__main__":
    unittest.main()
