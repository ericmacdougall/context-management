"""Offline test of runkit/leak_scan.py (review A, A4 and A6): every representation of every secret the stream creates
is found, non-secret controls are not, a hit exits 1, and an unreadable secret source exits 2. All values are FAKE,
generated here at run time; nothing prints a value (assertion messages name files and classes only).

The scanner under test runs as a subprocess, the way the run kit runs it: this tree's runkit/leak_scan.py, or the file
named in LEAK_SCAN_UNDER_TEST (runkit/failing_first.sh runs the dce5f07 blob, the scanner review A inspected). A fake
`docker` on PATH answers the key-store query (`docker exec <pg> psql ...`) with fake key hex.
usage (from writepath/): python3 -m unittest tests.test_leak_scan -v"""
import base64
import gzip
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parents[1]
SCANNER = os.environ.get("LEAK_SCAN_UNDER_TEST") or str(HERE / "runkit" / "leak_scan.py")


def _b64(x):
    return base64.b64encode(x.encode() if isinstance(x, str) else x).decode()


def _pw():
    """A token_urlsafe(32) password, as host/bootstrap.py, wp/setup.py and wp/es_keys.py make them."""
    return secrets.token_urlsafe(32)


def _kms():
    """A 32-byte key-store key whose standard base64 contains '+' or '/', so its base64url form differs from it."""
    while True:
        k = secrets.token_bytes(32)
        if set(_b64(k)) & set("+/"):
            return k


def _espw():
    """An Elasticsearch-style generated password with characters that percent-encoding changes."""
    alpha = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alpha) for _ in range(16)) + "@!" + "".join(secrets.choice(alpha) for _ in range(2))


class Scan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        app, admin, legacy, broker, espw = _pw(), _pw(), _pw(), _pw(), _espw()
        kid, key = secrets.token_urlsafe(15)[:20], secrets.token_urlsafe(16)[:22]
        enc = _b64(f"{kid}:{key}")
        kms = _kms()
        cls.values = [app, admin, legacy, broker, espw, key, enc, kms.hex()]
        home = cls.tmp / "home"
        for d in ("secrets", "oldhome", "conf"):
            (home / d).mkdir(parents=True)
        (home / "conf" / "pg_container").write_text("emlab2-wphard-pg\n")
        (home / "secrets" / "pg_password").write_text(admin)
        (home / "secrets" / "pgpass").write_text(f"127.0.0.1:25660:*:wp:{admin}\n")
        (home / "secrets" / "pgpass.wp_app").write_text(f"127.0.0.1:25660:*:wp_app:{app}\n")
        (home / "secrets" / "es_elastic_password").write_text(espw + "\n")
        (home / "secrets" / "es_broker_password").write_text(broker + "\n")
        (home / "secrets" / "es_api_key.verifier.json").write_text(json.dumps({"id": kid, "encoded": enc}))
        (home / "oldhome" / "pgpass").write_text(f"127.0.0.1:25660:*:emlab:{legacy}\n")
        cls.home = home
        doc = _b64(b"x" * 49 + app.encode() + b"y" * 50)
        cls.planted = {
            "pgpass_password_in_json": json.dumps({"dsn_password": app}),
            "admin_password_in_url": f"postgresql://wp:{admin}@127.0.0.1:25660/wp_events",
            "legacy_oldhome_password": f"emlab password {legacy}",
            "elastic_password_percent_encoded_url": "http://elastic:" + urllib.parse.quote(espw, safe="") + "@127.0.0.1:9200/",
            "basic_header_elastic": "Authorization: Basic " + _b64(f"elastic:{espw}"),
            "basic_header_broker": "Authorization: Basic " + _b64(f"wp16096_broker:{broker}"),
            "apikey_header_encoded": "Authorization: ApiKey " + enc,
            "apikey_decoded_id_key": f"{kid}:{key}",
            "apikey_key_alone": f'{{"api_key": "{key}"}}',
            "apikey_key_json_unicode_escaped": '{"api_key": "\\u%04x%s"}' % (ord(key[0]), key[1:]),
            "hex": app.encode().hex(),
            "hex_upper": app.encode().hex().upper(),
            "base64": _b64(app),
            "split_across_lines": app[:12] + "\n    " + app[12:],
            "split_in_json_string": '{"v": "' + app[:12] + "\\n" + app[12:] + '"}',
            "inside_wrapped_base64_document": "\n".join(doc[i:i + 76] for i in range(0, len(doc), 76)),
            "fragment_of_22_chars": "... " + broker[4:26] + " ...",
            "single_quoted_shell": f"export ES_PW='{espw}'",
            "keystore_key_hex": json.dumps({"key_hex": kms.hex()}),
            "keystore_key_hex_upper": kms.hex().upper(),
            "keystore_key_base64": _b64(kms),
            "keystore_key_base64url": base64.urlsafe_b64encode(kms).decode(),
            "keystore_key_raw_bytes": b"\x00\x01" + kms + b"\x02",
            "gzip_of_a_log": gzip.compress(f"line one\n{app}\nline three\n".encode()),
        }
        res = cls.tmp / "results"
        res.mkdir()
        for n, v in cls.planted.items():
            (res / n).write_bytes(v if isinstance(v, bytes) else v.encode())
        # non-secret controls: the key id (results record it by design), user names, hosts, base64 of the id
        (res / "controls_not_secret").write_text(json.dumps({"key_id": kid, "users": ["wp", "wp_app", "emlab", "elastic"],
                                                             "host": "127.0.0.1:25660", "id_b64": _b64(kid)}) + "\n")
        clean = cls.tmp / "clean"
        clean.mkdir()
        (clean / "result.json").write_text(json.dumps({"key_id": kid, "verdict": "PASS", "n": 5}))
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text('#!/bin/sh\n[ -e "$FAKE_KS_FAIL" ] && exit 1\ncat "$FAKE_KS"\n')
        (bindir / "docker").chmod(0o755)
        (cls.tmp / "keys.txt").write_text(kms.hex() + "\n" + secrets.token_bytes(32).hex() + "\n")
        cls.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_KS=str(cls.tmp / "keys.txt"),
                       FAKE_KS_FAIL=str(cls.tmp / "ks_fail"))
        cls.rc, cls.out = cls.run_scan(home, res)
        cls.hits = {pathlib.Path(ln.split()[1]).name for ln in cls.out.splitlines() if ln.startswith("HIT ")}

    @classmethod
    def run_scan(cls, home, *dirs, env=None):
        r = subprocess.run([sys.executable, SCANNER, str(home), *map(str, dirs)], capture_output=True, text=True,
                           env=env or cls.env, timeout=600)
        return r.returncode, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_each_representation_is_found(self):
        for name in self.planted:
            with self.subTest(representation=name):
                self.assertIn(name, self.hits, f"the scanner missed the planted representation {name}")

    def test_non_secret_controls_do_not_hit(self):
        self.assertNotIn("controls_not_secret", self.hits, "a key id, user name or host was treated as a secret")

    def test_a_hit_exits_1(self):
        self.assertEqual(self.rc, 1, "a scan with hits must exit 1 so the copy step can refuse")

    def test_no_value_or_fragment_is_printed(self):
        leaked = [i for i, v in enumerate(self.values) if any(v[j:j + 12] in self.out for j in range(0, len(v) - 11))]
        self.assertEqual(leaked, [], "the scanner output contains a fragment of a secret value (indexes only shown)")

    def test_clean_tree_exits_0(self):
        rc, out = self.run_scan(self.home, self.tmp / "clean")
        self.assertEqual(rc, 0, "a clean tree must exit 0")
        self.assertNotIn("HIT ", out)

    def test_unreadable_secret_source_fails_closed(self):
        home = self.tmp / "home_unreadable"
        shutil.copytree(self.home, home)
        p = home / "secrets" / "es_broker_password"
        p.chmod(0)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("running as a user who can read a mode-000 file")
            rc, _ = self.run_scan(home, self.tmp / "clean")
        finally:
            p.chmod(0o600)
        self.assertEqual(rc, 2, "an unreadable secret source must make the scan fail closed (exit 2)")

    def test_unreadable_key_store_fails_closed(self):
        (self.tmp / "ks_fail").touch()
        try:
            rc, _ = self.run_scan(self.home, self.tmp / "clean")
        finally:
            (self.tmp / "ks_fail").unlink()
        self.assertEqual(rc, 2, "an unreadable key store must make the scan fail closed (exit 2) unless waived")

    def test_self_test_runs_with_every_scan(self):
        if "LEAK_SCAN_UNDER_TEST" in os.environ:
            self.skipTest("the self-test is a feature of this tree's scanner (not counted as red)")
        self.assertRegex(self.out, r"self-test: (\d+)/\1 planted representations found, 0 control hit")

    def test_in_process_find_for_issuing_processes(self):
        if "LEAK_SCAN_UNDER_TEST" in os.environ:
            self.skipTest("library function of this tree's scanner (not counted as red)")
        sys.path.insert(0, str(HERE / "runkit"))
        import leak_scan
        key = secrets.token_urlsafe(16)[:22]
        kid = secrets.token_urlsafe(15)[:20]
        self.assertTrue(leak_scan.find(("ApiKey " + _b64(f"{kid}:{key}")).encode(), [key]))
        self.assertFalse(leak_scan.find(json.dumps({"id": kid}).encode(), [key]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
