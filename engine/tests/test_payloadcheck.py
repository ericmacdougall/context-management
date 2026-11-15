import json
import unittest

from engine import payloadcheck as pc


def ok(v):
    b = json.dumps(v).encode()
    try:
        pc.check_bytes(b, b"json/plain")
        return True
    except pc.PayloadRejected:
        return False


def broken_check(v):  # a variant that only checks size, as a naive implementation would
    return len(json.dumps(v).encode()) <= pc.MAX_BYTES


class PayloadCheckTests(unittest.TestCase):
    def test_accepts_ids_cursors_counters(self):
        self.assertTrue(ok({"sid": "s0001", "pend": ["t1/s0001/5", "t1/s0001/7"], "turns_done": 12, "ver": 2,
                            "rollup_s": 1.5, "approval": "waiting", "flag": True, "x": None}))

    def test_rejects_free_text(self):
        self.assertFalse(ok({"note": "the user said hello there"}))
        self.assertFalse(ok(["Please summarise the document"]))
        self.assertFalse(ok("a\nb"))

    def test_rejects_long_string_even_without_spaces(self):
        self.assertFalse(ok({"blob": "a" * 161}))

    def test_rejects_oversize_and_big_lists(self):
        self.assertFalse(ok(["x"] * 65))
        self.assertFalse(ok({f"k{i}": "v" for i in range(65)}))
        self.assertFalse(ok([["a" * 150] * 30] * 2))  # ids only, but > 4 KB

    def test_rejects_nesting_bad_keys_and_binary(self):
        self.assertFalse(ok({"a": {"b": {"c": {"d": {"e": 1}}}}}))
        self.assertFalse(ok({"has space": 1}))
        with self.assertRaises(pc.PayloadRejected):
            pc.check_bytes(b"\x00\x01", b"binary/plain")

    def test_size_only_variant_would_pass_prose(self):
        # the naive variant accepts free text, so the real check is what stops it
        prose = {"note": "free text prose with spaces"}
        self.assertTrue(broken_check(prose))
        self.assertFalse(ok(prose))


if __name__ == "__main__":
    unittest.main()
