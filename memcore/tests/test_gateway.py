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
                    {"messages": []}):
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


TOOL_BODY = {"model": "fake-1", "messages": [{"role": "user", "content": "x"}], "n": 3, "user": "u", "reasoning_effort": "low",
             "tools": [{"type": "function", "function": {"name": "answer", "parameters": SCHEMA}}],
             "tool_choice": {"type": "function", "function": {"name": "answer"}}}


class ElasticFacingTests(unittest.TestCase):
    """What Elasticsearch's openai service sends and parses (research, ES 9.5 source): see NOTES.md."""

    @classmethod
    def setUpClass(cls):
        from memcore.fakes import HashEmbedder
        d = tempfile.mkdtemp()
        cls.backend = FakeChat(lambda p, s, j: ({"a": 7} if j else "hello"), cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=50)
        cls.gw = Gateway({"fake-1": cls.backend}, os.path.join(d, "gw"), embedder=HashEmbedder(8))
        cls.gw.start()
        cls.tok = cls.gw.token

    @classmethod
    def tearDownClass(cls):
        cls.gw.shutdown()

    def post(self, path, body):
        req = urllib.request.Request(self.gw.base_url + path, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + self.tok}, method="POST")
        return urllib.request.urlopen(req)

    def test_validation_request_shape_nonstream_content_is_string(self):
        r = json.load(self.post("/chat/completions", {"model": "fake-1", "messages": [{"role": "user", "content": "how big"}], "n": 1, "stream": False}))
        self.assertIsInstance(r["choices"][0]["message"]["content"], str)
        self.assertEqual(r["choices"][0]["index"], 0)

    def test_forced_tool_nonstream_content_still_a_string_and_ignores_n_user_reasoning(self):
        r = json.load(self.post("/chat/completions", TOOL_BODY))
        m = r["choices"][0]["message"]
        self.assertEqual(m["content"], "")
        self.assertEqual(json.loads(m["tool_calls"][0]["function"]["arguments"]), {"a": 7})

    def test_stream_tool_deltas_and_final_usage_all_three_counts(self):
        raw = self.post("/chat/completions", dict(TOOL_BODY, stream=True, stream_options={"include_usage": True})).read()
        evs = list(parse_events(raw.splitlines(True)))
        self.assertEqual(evs[-1], "[DONE]")
        for e in evs[:-1]:
            for k in ("id", "object", "model"):
                self.assertTrue(e.get(k), (k, e))
            for c in e["choices"]:
                self.assertIn("index", c)
                self.assertIn("delta", c)
        usage = evs[-2]
        self.assertEqual(usage["choices"], [])
        self.assertEqual(sorted(k for k in usage["usage"] if k != "prompt_tokens_details"), ["completion_tokens", "prompt_tokens", "total_tokens"])
        tcs = [c["delta"]["tool_calls"][0] for e in evs[:-2] for c in e["choices"] if c["delta"].get("tool_calls")]
        self.assertTrue(all("index" in t for t in tcs))
        self.assertEqual(json.loads("".join(t["function"].get("arguments", "") for t in tcs)), {"a": 7})

    def test_unknown_model_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.post("/chat/completions", {"model": "nope", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(c.exception.code, 404)

    def test_embeddings_base64_float32(self):
        import base64, struct
        r = json.load(self.post("/embeddings", {"model": "m", "input": ["a b", "c"], "encoding_format": "base64"}))
        v = struct.unpack("<8f", base64.b64decode(r["data"][0]["embedding"]))
        self.assertEqual(len(v), 8)
        self.assertEqual(len(r["data"]), 2)
        r2 = json.load(self.post("/embeddings", {"input": "a b"}))
        self.assertEqual(len(r2["data"][0]["embedding"]), 8)

    def test_input_type_is_not_needed(self):
        # the Embedder port owns query/passage; the wire body never carries input_type
        self.assertEqual(len(json.load(self.post("/embeddings", {"input": "x"}))["data"]), 1)


class BindRulesTests(unittest.TestCase):
    def test_bridge_host_rules(self):
        d = tempfile.mkdtemp()
        b = FakeChat(lambda p, s, j: "x", cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=5)
        for bad in ("0.0.0.0", "8.8.8.8", "::", "192.168.1.20", "10.0.0.5"):
            with self.assertRaises(ValueError):
                Gateway(b, os.path.join(d, "g"), host=bad, allow_bridge_host=bad)
        with self.assertRaises(ValueError):
            Gateway(b, os.path.join(d, "g"), host="172.17.0.1")  # private but not loopback and not named as the bridge host

    def test_subscription_backend_is_refused_on_every_bind_even_with_the_dev_flag(self):
        """Decision 2026-11-16: subscription CLIs never sit behind an HTTP front (the ES inference API would call it)."""
        from memcore.gateway import SubscriptionBehindHttpRefused
        d = tempfile.mkdtemp()
        b = FakeChat(lambda p, s, j: "x", cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=5)
        b.subscription_backed = True
        for kw in ({}, {"dev_subscription_backends": True},
                   {"host": "172.17.0.1", "allow_bridge_host": "172.17.0.1", "dev_subscription_backends": True}):
            with self.assertRaises(SubscriptionBehindHttpRefused):
                Gateway(b, os.path.join(d, "g"), **kw)


class Delegating:
    """A wrapper around a subscription backend that does NOT forward the taint attribute (the review's reproducer)."""
    network_safe = False

    def __init__(self, inner):
        self.inner, self.model, self.capabilities = inner, inner.model, inner.capabilities

    def complete(self, *a, **k):
        return self.inner.complete(*a, **k)


class FailClosedTests(unittest.TestCase):
    """Review finding 2: the non-loopback gate used to fail OPEN."""

    def mk(self):
        d = tempfile.mkdtemp()
        b = FakeChat(lambda p, s, j: "x", cache_dir=os.path.join(d, "c"), ledger_path=os.devnull, cap=5)
        return d, b

    def test_wrapper_around_subscription_backend_refused_on_bridge(self):
        d, b = self.mk()
        b.subscription_backed = True
        for backend in (Delegating(b), b):
            with self.assertRaises(ValueError):
                Gateway(backend, os.path.join(d, "g"), host="172.17.0.1", allow_bridge_host="172.17.0.1", dev_subscription_backends=True)

    def test_loopback_client_backend_is_tainted(self):
        d, _ = self.mk()
        client = OpenAICompatChat("http://127.0.0.1:9/v1", "m", token="t", cache_dir=os.path.join(d, "cc"), ledger_path=os.devnull, cap=5)
        self.assertTrue(client.subscription_backed)
        from memcore.gateway import SubscriptionBehindHttpRefused
        with self.assertRaises(SubscriptionBehindHttpRefused):
            Gateway(client, os.path.join(d, "g"), dev_subscription_backends=True)   # loopback, even with the dev flag
        with self.assertRaises(ValueError):
            Gateway(client, os.path.join(d, "g"), host="172.17.0.1", allow_bridge_host="172.17.0.1", dev_subscription_backends=True)

    def test_undeclared_backend_refused_off_loopback_declared_stub_allowed_only_on_the_bridge_address(self):
        d, b = self.mk()
        class Plain:
            model, capabilities = "m", b.capabilities
        with self.assertRaises(ValueError):
            Gateway(Plain(), os.path.join(d, "g"), host="172.17.0.1", allow_bridge_host="172.17.0.1")
        for host in ("192.168.1.20", "169.254.1.1"):
            with self.assertRaises(ValueError):
                Gateway(b, os.path.join(d, "g"), host=host, allow_bridge_host=host)


class EmbedBudgetAndUsageTests(unittest.TestCase):
    def setUp(self):
        from memcore.fakes import HashEmbedder
        self.d = tempfile.mkdtemp()
        self.b = FakeChat(lambda p, s, j: "x", cache_dir=os.path.join(self.d, "c"), ledger_path=os.devnull, cap=5, reports_usage=False)
        self.seen = []
        emb = HashEmbedder(4)
        orig = emb._embed
        emb._embed = lambda texts, kind: (self.seen.append((texts, kind)), orig(texts, kind))[1]
        self.gw = Gateway(self.b, os.path.join(self.d, "g"), embedder=emb, embed_cap=2)
        self.gw.start()
        self.tok = open(os.path.join(self.d, "g", "gateway.token")).read().strip()

    def tearDown(self):
        self.gw.shutdown()

    def post(self, path, body):
        return urllib.request.urlopen(urllib.request.Request(self.gw.base_url + path, data=json.dumps(body).encode(), method="POST",
                                                             headers={"Authorization": "Bearer " + self.tok}))

    def test_embeddings_are_capped_ledgered_and_input_type_respected(self):
        self.post("/embeddings", {"input": "a"})
        self.post("/embeddings", {"input": "b", "input_type": "search"})
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.post("/embeddings", {"input": "c"})
        self.assertEqual(c.exception.code, 429)
        self.assertEqual([k for _, k in self.seen], ["passage", "query"])
        self.assertEqual(len(open(os.path.join(self.d, "g", "embed-ledger.jsonl")).read().splitlines()), 2)

    def test_unknown_usage_is_omitted_not_zero(self):
        r = json.load(self.post("/chat/completions", {"model": "fake-1", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertNotIn("usage", r)
        raw = self.post("/chat/completions", {"model": "fake-1", "messages": [{"role": "user", "content": "hi2"}], "stream": True,
                                              "stream_options": {"include_usage": True}}).read()
        evs = list(parse_events(raw.splitlines(True)))
        self.assertFalse(evs[-2]["usage"]["x_memcore_usage_known"])


if __name__ == "__main__":
    unittest.main()
