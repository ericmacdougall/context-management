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
