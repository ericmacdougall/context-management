import os
import tempfile
import time
import unittest

from memcore.chat_base import pack_batch_prompt, run_child
from memcore.fakes import FakeChat
from memcore.ports import CallCapExceeded, CallTimeout, InputTooLong, SchemaError

U = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}


class Base(unittest.TestCase):
    def mk(self, fn, cap=10, **kw):
        d = tempfile.mkdtemp()
        return FakeChat(fn, cache_dir=os.path.join(d, "c"), ledger_path=os.path.join(d, "l.jsonl"), cap=cap, **kw)


class CacheCapTests(Base):
    def test_hash_deterministic_and_sensitive(self):
        m = self.mk(lambda p, s, j: "x")
        h = m.request_hash(U, system="s")
        self.assertEqual(h, m.request_hash(list(U), system="s"))
        self.assertNotEqual(h, m.request_hash(U, system="t"))
        self.assertNotEqual(h, m.request_hash(U, system="s", json_schema=SCHEMA))
        self.assertNotEqual(h, m.request_hash(U, system="s", temperature=0.0))

    def test_cache_hit_spends_nothing(self):
        m = self.mk(lambda p, s, j: "x")
        a, b = m.complete(U), m.complete(U)
        self.assertFalse(a.cached)
        self.assertTrue(b.cached)
        self.assertEqual((m.backend_calls, m.calls_used()), (1, 1))
        self.assertEqual(a.request_hash, b.request_hash)

    def test_broken_variant_without_cache_is_detected(self):
        m = self.mk(lambda p, s, j: "x")
        m._load = lambda key: None  # broken: cache never hits
        m.complete(U)
        m.complete(U)
        self.assertEqual(m.backend_calls, 2)  # the assertion in test_cache_hit_spends_nothing would fail on this

    def test_cap_enforced_before_backend(self):
        m = self.mk(lambda p, s, j: "x", cap=1)
        m.complete([{"role": "user", "content": "a"}])
        with self.assertRaises(CallCapExceeded):
            m.complete([{"role": "user", "content": "b"}])
        self.assertEqual(m.backend_calls, 1)
        m.complete([{"role": "user", "content": "a"}])  # cached: still fine at the cap

    def test_failure_not_cached_but_counted(self):
        def boom(p, s, j):
            raise RuntimeError("x")
        m = self.mk(boom)
        with self.assertRaises(RuntimeError):
            m.complete(U)
        self.assertFalse(os.path.exists(m._path(m.request_hash(U))))
        self.assertEqual(m.calls_used(), 1)

    def test_usage_none_when_not_reported(self):
        self.assertIsNone(self.mk(lambda p, s, j: "x", reports_usage=False).complete(U).usage)
        self.assertEqual(self.mk(lambda p, s, j: "xxxxxxxx").complete(U).usage.output_tokens, 2)

    def test_overlong_input_zero_calls(self):
        m = self.mk(lambda p, s, j: "x")
        with self.assertRaises(InputTooLong):
            m.complete([{"role": "user", "content": "y" * 5000}])
        self.assertEqual((m.backend_calls, m.calls_used()), (0, 0))

    def test_cached_result_roundtrips_usage_and_parsed(self):
        m = self.mk(lambda p, s, j: {"a": 1})
        m.complete(U, json_schema=SCHEMA)
        r = m.complete(U, json_schema=SCHEMA)
        self.assertEqual((r.cached, r.parsed, r.usage.output_tokens is not None), (True, {"a": 1}, True))


class SchemaRepairTests(Base):
    def test_repair_once_then_ok(self):
        seq = iter([{"a": "no"}, {"a": 2}])
        m = self.mk(lambda p, s, j: next(seq))
        r = m.complete(U, json_schema=SCHEMA)
        self.assertEqual((r.parsed, m.backend_calls, r.meta.get("repaired")), ({"a": 2}, 2, True))

    def test_second_failure_raises_and_not_cached(self):
        m = self.mk(lambda p, s, j: {"a": "no"})
        with self.assertRaises(SchemaError):
            m.complete(U, json_schema=SCHEMA)
        self.assertEqual(m.backend_calls, 2)  # exactly one repair, no loop
        self.assertFalse(os.path.exists(m._path(m.request_hash(U, json_schema=SCHEMA))))

    def test_fenced_json_accepted(self):
        m = self.mk(lambda p, s, j: '```json\n{"a": 3}\n```')
        self.assertEqual(m.complete(U, json_schema=SCHEMA).parsed, {"a": 3})


class ChildTests(unittest.TestCase):
    def test_timeout_kills_group(self):
        t0 = time.time()
        with self.assertRaises(CallTimeout):
            run_child(["sh", "-c", "sleep 30"], "", 0.5, None)
        self.assertLess(time.time() - t0, 5)

    def test_broken_variant_no_timeout_would_hang(self):
        p = run_child(["sh", "-c", "cat"], "echo", 5, None)  # stdin delivered verbatim
        self.assertEqual(p.stdout, "echo")

    def test_batch_pack(self):
        s = pack_batch_prompt("Do X", ["a", "b"])
        self.assertIn("exactly 2 elements", s)
        self.assertIn("### ITEM 2\nb", s)


if __name__ == "__main__":
    unittest.main()


class FilterAndKeyTests(Base):
    def test_cache_key_includes_adapter_model_recipe(self):
        a = self.mk(lambda p, s, j: "x")
        b = self.mk(lambda p, s, j: "x")
        b.model = "other-model"
        c = self.mk(lambda p, s, j: "x", recipe="v2")
        h = a.request_hash(U)
        self.assertNotEqual(h, b.request_hash(U))
        self.assertNotEqual(h, c.request_hash(U))
        b.adapter_id = "other-adapter"
        self.assertNotEqual(self.mk(lambda p, s, j: "x").request_hash(U), b.request_hash(U))

    def test_post_filter_strips_banner_counts_and_default_off(self):
        text = "42 (you asked for 41). (Heads-up: the log backup is 50h old)"
        off = self.mk(lambda p, s, j: text)
        self.assertEqual(off.complete(U).text, text)           # default: nothing filtered
        on = self.mk(lambda p, s, j: text, strip_patterns=[r"\(Heads-up:[^)]*\)"])
        r = on.complete(U)
        self.assertEqual((r.text, on.filter_hits, r.meta["filtered"]), ("42 (you asked for 41).", 1, 1))

    def test_suppress_sentence_is_opt_in(self):
        seen = []
        m = self.mk(lambda p, s, j: seen.append(s) or "x")
        m.complete(U)
        self.assertIsNone(seen[0])
        m2 = self.mk(lambda p, s, j: seen.append(s) or "x", suppress_harness_notices=True)
        m2.complete(U)
        self.assertIn("session-start notices", seen[1])
