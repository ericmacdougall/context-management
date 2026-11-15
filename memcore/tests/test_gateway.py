import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from memcore.adapters.openai_compat import OpenAICompatChat
from memcore.fakes import FakeChat
from memcore.gateway import Gateway, require_loopback, to_port_request
from memcore.ports import BackendError, InputTooLong
from memcore.sse import chat_chunks, frame, parse_events

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}


class SseTests(unittest.TestCase):
    def test_roundtrip_with_final_usage_chunk(self):
        chunks = chat_chunks("id1", "m", 1, "hello world " * 10, {"prompt_tokens": 3, "completion_tokens": 4}, piece=10)
        evs = list(parse_events(b"".join(chunks).splitlines(True)))
        self.assertEqual(evs[-1], "[DONE]")
        self.assertEqual(evs[-2]["choices"], [])
        self.assertEqual(evs[-2]["usage"]["completion_tokens"], 4)
        self.assertEqual(evs[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(evs[-3]["choices"][0]["finish_reason"], "stop")
        self.assertEqual("".join(e["choices"][0]["delta"].get("content", "") for e in evs[:-2] if e["choices"]), "hello world " * 10)

    def test_every_event_ends_with_blank_line(self):
        self.assertTrue(frame({"a": 1}).endswith(b"\n\n"))

    def test_broken_framing_detected(self):
        broken = b'data: {"a":1}\n'  # no blank line: a conforming parser must not accept it as complete
        with self.assertRaises(ValueError):
            list(parse_events(broken.splitlines(True)))


class MappingTests(unittest.TestCase):
    def test_mapping(self):
        kw = to_port_request({"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                              "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": SCHEMA}}, "max_tokens": 5})
        self.assertEqual((kw["system"], kw["json_schema"], kw["max_output_tokens"]), ("S", SCHEMA, 5))

    def test_unsupported_rejected(self):
        for bad in ({"messages": [{"role": "tool", "content": "x"}]}, {"messages": [{"role": "user", "content": "x"}], "tools": [{}]},
                    {"messages": []}, {"messages": [{"role": "user", "content": "x"}], "n": 2}):
            with self.assertRaises(ValueError):
                to_port_request(bad)


class LoopbackTests(unittest.TestCase):
    def test_refuses_non_loopback(self):
        for h in ("0.0.0.0", "192.168.1.5", "localhost", "::", ""):
            with self.assertRaises(ValueError):
                require_loopback(h)
        require_loopback("127.0.0.1")

    def test_broken_variant_gateway_constructor_refuses(self):
        with self.assertRaises(ValueError):
            Gateway(FakeChat(lambda p, s, j: "x", cache_dir=tempfile.mkdtemp(), ledger_path=os.devnull, cap=5), tempfile.mkdtemp(), host="0.0.0.0")


class GatewayLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        cls.backend = FakeChat(lambda p, s, j: ({"a": 7} if j else "echo:%s|%s" % (s, p)), cache_dir=os.path.join(d, "c"),
                               ledger_path=os.path.join(d, "l"), cap=50)
        cls.gw = Gateway(cls.backend, os.path.join(d, "gw"))
        cls.gw.start()
        cls.token = open(os.path.join(d, "gw", "gateway.token")).read()
        cls.d = d

    @classmethod
    def tearDownClass(cls):
        cls.gw.shutdown()

    def client(self):
        return OpenAICompatChat(self.gw.base_url, "fake-1", token=self.token, cache_dir=tempfile.mkdtemp(), ledger_path=os.devnull,
                                cap=100, max_context_tokens=None)

    def test_files_and_bind(self):
        self.assertEqual(self.gw.host, "127.0.0.1")
        for n in ("gateway.token", "gateway.port"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, "gw", n)).st_mode), 0o600)

    def test_auth_required(self):
        req = urllib.request.Request(self.gw.base_url + "/models", headers={"Authorization": "Bearer wrong"})
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req)
        self.assertEqual(c.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.gw.base_url + "/models")

    def test_models_and_completion_with_system_and_usage(self):
        req = urllib.request.Request(self.gw.base_url + "/models", headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(json.load(urllib.request.urlopen(req))["data"][0]["id"], "fake-1")
        r = self.client().complete([{"role": "user", "content": "hi"}], system="SYS")
        self.assertEqual(r.text, "echo:SYS|hi")
        self.assertIsNotNone(r.usage.output_tokens)

    def test_schema_through_gateway(self):
        self.assertEqual(self.client().complete([{"role": "user", "content": "x"}], json_schema=SCHEMA).parsed, {"a": 7})

    def test_streaming_simulated_with_usage(self):
        parts, usage = [], None
        for t, u in self.client().stream([{"role": "user", "content": "stream me " * 20}], system="S"):
            parts.append(t)
            usage = u or usage
        self.assertEqual("".join(parts), "echo:S|" + "stream me " * 20)
        self.assertIsNotNone(usage)

    def test_gateway_spends_backend_cap_and_caches(self):
        before = self.backend.backend_calls
        c = self.client()
        c.complete([{"role": "user", "content": "cap test"}])
        c2 = self.client()  # fresh client cache, so the gateway sees it again: backend cache must hit
        c2.complete([{"role": "user", "content": "cap test"}])
        self.assertEqual(self.backend.backend_calls - before, 1)

    def test_overlong_maps_to_input_too_long(self):
        with self.assertRaises(InputTooLong):
            self.client().complete([{"role": "user", "content": "y" * 5000}])  # fake backend max is 1000 tokens

    def test_bad_request_is_clean_400(self):
        req = urllib.request.Request(self.gw.base_url + "/chat/completions", data=b'{"messages":[{"role":"tool","content":"x"}]}',
                                     headers={"Authorization": "Bearer " + self.token}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req)
        self.assertEqual(c.exception.code, 400)


if __name__ == "__main__":
    unittest.main()


class GatewayCapTest(unittest.TestCase):
    def test_cap_returns_429_and_no_extra_backend_call(self):
        d = tempfile.mkdtemp()
        b = FakeChat(lambda p, s, j: "x", cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=1)
        gw = Gateway(b, os.path.join(d, "gw"))
        gw.start()
        try:
            tok = open(os.path.join(d, "gw", "gateway.token")).read()
            cl = OpenAICompatChat(gw.base_url, "fake-1", token=tok, cache_dir=os.path.join(d, "cc"), ledger_path=os.devnull, cap=100)
            cl.complete([{"role": "user", "content": "a"}])
            with self.assertRaises(BackendError) as c:
                cl.complete([{"role": "user", "content": "b"}])
            self.assertIn("429", str(c.exception))
            self.assertEqual(b.backend_calls, 1)
        finally:
            gw.shutdown()
