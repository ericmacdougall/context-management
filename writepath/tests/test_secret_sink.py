"""Offline test of wp/secret_sink.py (GATE-DESIGN 7.6, review F scanner half, option 2: prevention at source): a value
registered by the process that holds it cannot be written through the sink in any representation the scanner uses,
a clean write lands atomically with its mode, log records holding a value are redacted whole, and nothing the sink
raises or logs holds a value. All values are FAKE, generated here; assertion messages name labels and classes only.
The module under test is wp/secret_sink.py, or the file named in SECRET_SINK_UNDER_TEST (a broken variant, for the
failing-first receipt).
usage (from writepath/): python3 -m unittest tests.test_secret_sink -v"""
import base64
import importlib.util
import io
import json
import logging
import os
import pathlib
import secrets
import shutil
import sys
import tempfile
import unittest
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parents[1]
SINK = os.environ.get("SECRET_SINK_UNDER_TEST") or str(HERE / "wp" / "secret_sink.py")


def _url(scheme, user, pw, rest):
    """scheme://user:pw@rest, built from parts so that no source line has the shape of a credential URL (the public
    repository's pre-push scan); pw is a fake value generated at run time."""
    return scheme + "://" + user + ":" + pw + "@" + rest


def _load():
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(f"secret_sink_under_test_{secrets.token_hex(3)}", SINK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sink(unittest.TestCase):
    def setUp(self):
        self.s = _load()                       # a fresh module: a fresh registry
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pw = secrets.token_urlsafe(32)
        self.kid, self.key = secrets.token_urlsafe(15)[:20], secrets.token_urlsafe(16)[:22]
        self.enc = base64.b64encode(f"{self.kid}:{self.key}".encode()).decode()
        self.s.register(self.pw, "test password")
        self.s.register(self.enc, "test api key", complete=True)
        self.s.register(f"{self.kid}:{self.key}", "test api key", complete=True)
        self.s.register(self.key, "test api key")

    def leaked(self, text):
        vals = [self.pw, self.key, self.enc]
        return [i for i, v in enumerate(vals) if any(v[j:j + 12] in text for j in range(len(v) - 11))]

    def refused(self, data, name="out.json"):
        d = self.tmp / f"w-{name}"               # one directory per write: one case cannot fail another
        d.mkdir()
        p = d / name
        with self.assertRaises(self.s.SecretInOutput) as cm:
            self.s.write(p, data)
        self.assertFalse(p.exists(), "a refused write left a file")
        self.assertFalse(list(d.iterdir()), "a refused write left a temporary file")
        self.assertEqual(self.leaked(str(cm.exception)), [], "the refusal holds a value")
        return cm.exception

    # ---- refusal in every representation the scanner uses
    def test_raw_value_is_refused(self):
        e = self.refused(json.dumps({"dsn": _url("postgresql", "wp", self.pw, "127.0.0.1/x")}))
        self.assertIn("test password", str(e), "the refusal does not name the label")

    def test_encoded_forms_are_refused(self):
        forms = {"base64": base64.b64encode(self.pw.encode()).decode(), "hex": self.pw.encode().hex(),
                 "percent": "".join("%%%02X" % b for b in self.pw.encode()),
                 "percent_url": _url("http", "elastic", urllib.parse.quote(self.pw, safe=""), "127.0.0.1:9200/"),
                 "unicode_escaped": "".join("\\u%04x" % ord(c) for c in self.pw),
                 "split": self.pw[:12] + "\n   " + self.pw[12:], "fragment": "..." + self.pw[3:22] + "...",
                 "apikey_header": "Authorization: ApiKey " + self.enc, "key_alone": f'{{"k": "{self.key}"}}'}
        for n, v in forms.items():
            with self.subTest(form=n):
                self.refused(v, name=f"{n}.txt")

    def test_bytes_are_checked_like_text(self):
        self.refused(f"x {self.pw}".encode("utf-16-le"))

    def test_public_key_id_alone_is_not_refused(self):
        p = self.tmp / "ok.json"
        self.s.write(p, json.dumps({"key_id": self.kid, "verdict": "PASS"}))
        self.assertTrue(p.exists(), "a complete-class value matched on its public id")

    # ---- a clean write
    def test_clean_write_lands_with_its_mode(self):
        p = self.tmp / "r.json"
        self.s.write(p, json.dumps({"verdict": "PASS"}), mode=0o640)
        self.assertEqual(json.loads(p.read_text()), {"verdict": "PASS"})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir()), ["r.json"], "a temporary file was left")

    def test_unsearchable_content_is_refused(self):
        with self.assertRaises(self.s.SinkError):
            self.s.write(self.tmp / "x.gz", b"\x1f\x8b\x08\x00" + secrets.token_bytes(64))
        self.assertFalse((self.tmp / "x.gz").exists())

    # ---- find and the registry
    def test_find_returns_labels_only(self):
        self.assertEqual(self.s.find(f"pw={self.pw}"), ["test password"])
        self.assertEqual(self.s.find("nothing here"), [])
        self.assertEqual(self.s.labels(), ["test api key", "test password"])

    def test_label_holding_a_value_is_refused(self):
        with self.assertRaises(ValueError):
            self.s.register(secrets.token_urlsafe(32), f"label {self.pw}")

    # ---- logging
    def test_log_record_holding_a_value_is_redacted_whole(self):
        buf = io.StringIO()
        log = logging.getLogger(f"sinktest.{secrets.token_hex(3)}")
        log.propagate = False
        h = logging.StreamHandler(buf)
        h.addFilter(self.s.RedactingFilter())
        log.addHandler(h)
        log.warning("connected with %s", base64.b64encode(f"elastic:{self.pw}".encode()).decode())
        log.warning("verdict %s", "PASS")
        try:
            raise RuntimeError("bad dsn " + _url("postgresql", "wp", self.pw, "h/x"))
        except RuntimeError:
            log.exception("failed")
        out = buf.getvalue()
        self.assertEqual(self.leaked(out), [], "a log line holds a value")
        self.assertIn("verdict PASS", out, "a clean log line was changed")
        self.assertIn("test password", out, "a redacted line does not name the label")


if __name__ == "__main__":
    unittest.main(verbosity=2)
