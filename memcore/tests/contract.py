"""One contract suite for every ChatModel adapter. Subclass ChatModelContract, implement make_model(), and
(optionally) set OVERLONG_CHARS / SKIP_OVERLONG. Results of the schema run (k of 20) are written to
$MEMCORE_OUT/results/<name>.json. Budget per adapter: 1 plain + 1 system + 20 schema + 1 timeout = 23 model calls;
repeat and over-long cost 0."""
from __future__ import annotations

import json
import os
import time
import unittest

from memcore.jsonschema_min import validate
from memcore.ports import CallTimeout, ChatModel, InputTooLong, PortError, Usage

OUT = os.environ.get("MEMCORE_OUT", "/Users/eric/.agent-harness/work/73716864-6b3b-4deb-94ef-8ed2c8432e4a/memcore-out")
# Evidence files (results/<adapter>.json under OUT) are written ONLY for a deliberate evidence run (MEMCORE_EVIDENCE=1), and an
# existing evidence file is never overwritten (a timestamped sibling is written instead). Unit-test runs write to a temp dir.
EVIDENCE = os.environ.get("MEMCORE_EVIDENCE") == "1"

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["person", "city", "amount", "tags"],
          "properties": {"person": {"type": "string"}, "city": {"type": "string"}, "amount": {"type": "integer"},
                         "tags": {"type": "array", "items": {"type": "string"}}}}

_NAMES = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun"]
_CITIES = ["Lisbon", "Osaka", "Denver", "Cairo", "Oslo", "Lima", "Perth", "Quito", "Riga", "Seoul"]
_WORDS = ["invoice", "renewal", "refund", "deposit", "pledge", "grant", "rent", "tuition", "repair", "ticket"]


def extraction_prompts(n=20):
    out = []
    for i in range(n):
        p, c, a, w = _NAMES[i % 10], _CITIES[(i * 3) % 10], 100 + 37 * i, _WORDS[(i * 7) % 10]
        out.append(({"person": p, "city": c, "amount": a},
                    "Extract the person, the city, the amount as an integer, and 1 to 3 short topic tags.\n"
                    "Text: %s travelled to %s and paid %d dollars for a %s." % (p, c, a, w)))
    return out


class ChatModelContract:
    NAME = "adapter"
    OVERLONG_CHARS = 800_000
    SKIP_OVERLONG = False
    TIMEOUT_S = 1.0

    def make_model(self) -> ChatModel:
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.m = cls().make_model()
        cls.results = {"adapter": cls.NAME, "capabilities": vars(cls.m.capabilities)}

    @classmethod
    def tearDownClass(cls):
        import tempfile
        import time as _t
        d = os.path.join(OUT, "results") if EVIDENCE else os.path.join(tempfile.gettempdir(), "memcore-results")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, cls.NAME + ".json")
        if os.path.exists(p) and EVIDENCE:
            p = os.path.join(d, "%s.%d.json" % (cls.NAME, int(_t.time())))
        with open(p, "w") as fh:
            json.dump(cls.results, fh, indent=1, default=str)

    def U(self, text):
        return [{"role": "user", "content": text}]

    def test_01_plain_completion(self):
        r = self.m.complete(self.U("What is the capital of France? Answer with one word."))
        self.assertIn("paris", r.text.lower())
        self.assertTrue(r.model)
        self.assertGreater(r.latency_s, 0)
        self.assertEqual(len(r.request_hash), 64)
        self.assertIsNone(r.parsed)

    def test_02_system_prompt_honoured(self):
        r = self.m.complete(self.U("What is the capital of France?"), system="Whatever the user asks, reply with exactly the single word PINEAPPLE and nothing else.")
        self.assertIn("pineapple", r.text.lower())
        self.assertNotIn("paris", r.text.lower())

    def test_03_json_schema_k_of_20(self):
        cases = extraction_prompts(20)
        reqs = [{"messages": self.U(p), "json_schema": SCHEMA} for _, p in cases]
        out = self.m.complete_many(reqs, max_workers=3)
        valid, correct, errors = 0, 0, []
        for (gold, _), r in zip(cases, out):
            if isinstance(r, Exception):
                errors.append("%s: %s" % (type(r).__name__, str(r)[:120]))
                continue
            if r.parsed is not None and not validate(r.parsed, SCHEMA):
                valid += 1
                if all(r.parsed[k] == v for k, v in gold.items()):
                    correct += 1
        self.results["schema_valid_k_of_20"] = valid
        self.results["schema_fields_correct_k_of_20"] = correct
        self.results["schema_errors"] = errors[:5]
        self.results["json_schema_mode"] = self.m.capabilities.json_schema_mode
        self.assertGreaterEqual(valid, 19, "schema-valid %d/20; errors %s" % (valid, errors[:3]))

    def test_04_hash_deterministic_and_cache_hit_on_repeat(self):
        msgs = self.U("What is the capital of France? Answer with one word.")
        h1, h2 = self.m.request_hash(msgs), self.m.request_hash(list(msgs))
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, self.m.request_hash(msgs, system="x"))
        r1 = self.m.complete(msgs)  # cached by test_01 (or made here)
        r2 = self.m.complete(msgs)
        self.assertEqual(r1.request_hash, h1)
        self.assertTrue(r2.cached)
        self.assertEqual(r1.text, r2.text)

    def test_05_usage_present_or_explicit_none(self):
        r = self.m.complete(self.U("What is the capital of France? Answer with one word."))
        if self.m.capabilities.reports_usage:
            self.assertIsInstance(r.usage, Usage)
            self.assertIsInstance(r.usage.output_tokens, int)
            self.assertIsInstance(r.usage.input_tokens, int)
        else:
            self.assertIsNone(r.usage)
        self.results["usage_example"] = None if r.usage is None else vars(r.usage)

    def test_06_overlong_input_clean_error(self):
        if self.SKIP_OVERLONG or self.m.capabilities.max_context_tokens is None and not self.SKIP_OVERLONG and False:
            self.skipTest("declared")
        t0 = time.time()
        with self.assertRaises(InputTooLong):
            self.m.complete(self.U("word " * (self.OVERLONG_CHARS // 5)))
        self.assertLess(time.time() - t0, 30)

    def test_07_timeout_is_clean(self):
        t0 = time.time()
        with self.assertRaises(CallTimeout):
            self.m.complete(self.U("Write a 400 word story about a lighthouse keeper. Take your time %s." % self.NAME), timeout_s=self.TIMEOUT_S)
        self.assertLess(time.time() - t0, 20)
