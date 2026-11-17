import io
import os
import tempfile
import unittest

from memcore.adapters.elastic_inference import ElasticInferenceChat
from memcore.ports import BackendError
from memcore.tests.contract import ChatModelContract
from memcore.tests.es_stub import Stub

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["person", "city", "amount", "tags"],
          "properties": {"person": {"type": "string"}, "city": {"type": "string"}, "amount": {"type": "integer"},
                         "tags": {"type": "array", "items": {"type": "string"}}}}
U = [{"role": "user", "content": "Text: Ana travelled to Lisbon and paid 100 dollars for a invoice."}]


def mk(stub, **kw):
    d = tempfile.mkdtemp()
    return ElasticInferenceChat(stub.url, "mc-test", lambda: "ApiKey test", cache_dir=os.path.join(d, "c"), ledger_path=os.path.join(d, "l"), cap=500, **kw)


class StubShapeTests(unittest.TestCase):
    def test_bom_bare_chunks_forced_tool_and_strict_body(self):
        s = Stub()
        try:
            r = mk(s).complete(U, json_schema=SCHEMA)
            self.assertEqual(r.parsed["city"], "Lisbon")
            body = s.requests[0]["body"]
            self.assertEqual(sorted(body), ["messages", "tool_choice", "tools"])         # no response_format, nothing unknown
            self.assertEqual(s.requests[0]["path"], "/_inference/chat_completion/mc-test/_stream")
            self.assertIsNotNone(r.usage.output_tokens)
        finally:
            s.stop()

    def test_wrapped_chunks_without_bom(self):
        s = Stub(wrapped=True, bom=False)
        try:
            self.assertEqual(mk(s).complete(U, json_schema=SCHEMA).parsed["amount"], 100)
        finally:
            s.stop()

    def test_error_event_surfaces(self):
        s = Stub(error_after=1)
        try:
            with self.assertRaises(BackendError) as c:
                mk(s).complete(U)
            self.assertIn("boom", str(c.exception))
        finally:
            s.stop()

    def test_broken_variant_strict_parser_rejects_response_format(self):
        # control: a client that sent response_format (as the openai client does) would get a 400 from the strict stub
        import json, urllib.request, urllib.error
        s = Stub()
        try:
            req = urllib.request.Request(s.url + "/_inference/chat_completion/x/_stream", data=json.dumps({"messages": [], "response_format": {}}).encode(),
                                         headers={"Authorization": "ApiKey test"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req)
            self.assertEqual(c.exception.code, 400)
        finally:
            s.stop()

    def test_auth_header_not_in_cache_or_ledger(self):
        s = Stub()
        try:
            m = mk(s)
            m.complete(U)
            blob = "".join(open(os.path.join(m.cache_dir, f)).read() for f in os.listdir(m.cache_dir) if f.endswith(".json")) + open(m.ledger_path).read()
            self.assertNotIn("ApiKey", blob)
        finally:
            s.stop()


class TruncationTests(unittest.TestCase):
    """Review finding 8: a short stream used to be cached as a successful answer."""

    def test_truncated_stream_is_an_error_and_never_cached(self):
        s = Stub(truncate_after=1)
        try:
            m = mk(s)
            with self.assertRaises(BackendError):
                m.complete(U)
            self.assertEqual([f for f in os.listdir(m.cache_dir) if f.endswith(".json")], [])
            self.assertEqual(m.calls_used(), 1)
        finally:
            s.stop()

    def test_abrupt_cut_and_bad_json_are_backend_errors_not_raw_exceptions(self):
        import http.client
        m = mk(Stub())

        class Cut:
            def __iter__(self):
                yield b'event: message\ndata: {"id":"1","choices":[{"index":0,"delta":{"content":"The capital is "}}]}\n'
                raise http.client.IncompleteRead(b"x")
        with self.assertRaises(BackendError):
            m.read_stream(Cut())
        with self.assertRaises(BackendError):
            m.read_stream([b"event: message\n", b"data: {not json\n", b"\n"])

    def test_finish_reason_alone_counts_as_complete(self):
        m = mk(Stub())
        out = m.read_stream([b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n', b"\n"])
        self.assertEqual(out.text, "ok")

    def test_bom_is_stripped_so_an_error_event_is_seen(self):
        m = mk(Stub())
        raw = ("\ufeffevent: error\ndata: {\"error\":{\"message\":\"boom\"}}\n\n").encode("utf-8")
        with self.assertRaises(BackendError) as c:
            m.read_stream(io.BytesIO(raw))
        self.assertIn("boom", str(c.exception))

    def test_broken_variant_parser_without_bom_strip_misses_the_error(self):
        raw = ("\ufeffevent: error\ndata: {\"error\":{\"message\":\"boom\"}}\n\n").encode("utf-8")
        from memcore.sse import parse_named_events

        def naive(lines):                                  # no BOM strip: 'event:' is not recognised on the first line
            name, data = None, []
            for raw_line in lines:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if line == "":
                    yield (name or "message", "\n".join(data)); name, data = None, []
                elif line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
        self.assertEqual(list(naive(io.BytesIO(raw)))[0][0], "message")          # the naive parser misreports the error as a message
        self.assertEqual(list(parse_named_events(io.BytesIO(raw)))[0][0], "error")


_stub = None


def setUpModule():
    global _stub
    _stub = Stub()


def tearDownModule():
    _stub.stop()


class ElasticStubContract(ChatModelContract, unittest.TestCase):
    NAME = "elastic-inference(stub)"

    def make_model(self):
        return mk(_stub, max_context_tokens=1000)  # 3000 chars preflight: the 800k-char over-long input fails with 0 calls

    OVERLONG_CHARS = 800_000


if __name__ == "__main__":
    unittest.main()
