"""The search behind results/failing-first/harness/m3_pad_log.py (review J M3), offline: it must find a pad that is in
the log in the forms Postgres writes it, and nothing else.

  P1  a pad inlined as a bytea literal ('\\x<hex>' in a STATEMENT line, as client-side binding writes it): found
  P2  the same statement with server-side placeholders ($1, $2, $3) and no parameters line: not found
  P3  upper-case hex, and a pad inside a longer line with other hex around it but not adjacent: found
  P4  another pad's hex in the log: not counted for this pad"""
import hashlib
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "results" / "failing-first" / "harness"))

import m3_pad_log as M  # noqa: E402


def _h(pad):
    return hashlib.sha256(pad.hex().encode()).hexdigest()


class PadSearch(unittest.TestCase):
    def setUp(self):
        self.ip, self.op = os.urandom(64), os.urandom(64)

    def test_p1_inlined_bytea_found(self):
        log = (b"2026-11-19 ERROR:  seal key malformed\n2026-11-19 STATEMENT:  select wp_seal.install('', '\\x"
               + self.ip.hex().encode() + b"'::bytea, '\\x" + self.op.hex().encode() + b"'::bytea)\n")
        self.assertEqual(M.pad_hits(log, [_h(self.ip), _h(self.op)]), 2)

    def test_p2_placeholders_not_found(self):
        log = b"ERROR:  seal key malformed\nSTATEMENT:  select wp_seal.install($1, $2, $3)\n"
        self.assertEqual(M.pad_hits(log, [_h(self.ip), _h(self.op)]), 0)

    def test_p3_upper_case_in_a_longer_line(self):
        log = b"DETAIL: parameters: $1 = '', $2 = '\\x" + self.ip.hex().upper().encode() + b"', $3 = 'abc'\n"
        self.assertEqual(M.pad_hits(log, [_h(self.ip)]), 1)

    def test_p4_other_pad_not_counted(self):
        log = b"STATEMENT: '\\x" + os.urandom(64).hex().encode() + b"'\n"
        self.assertEqual(M.pad_hits(log, [_h(self.ip), _h(self.op)]), 0)


if __name__ == "__main__":
    unittest.main()
