"""Offline test of runkit/leak_scan.py (review A, A4 and A6; review D F4, F5, F6, F12): every representation of every
secret the stream creates is found, non-secret controls are not, a hit exits 1, anything the scanner could not read or
chose not to read exits 2, and nothing printed holds a candidate. All values are FAKE, generated here at run time;
nothing prints a value (assertion messages name files and classes only).

The scanner under test runs as a subprocess, the way the run kit runs it: this tree's runkit/leak_scan.py, or the file
named in LEAK_SCAN_UNDER_TEST (runkit/failing_first.sh runs the dce5f07 blob, the scanner review A inspected). A fake
`docker` on PATH answers the key-store queries (`docker exec <pg> psql ...`) with fake key hex: subject keys for a
query naming subject_keys, tenant keys for one naming tenant_keys (both for the older single query).
usage (from writepath/): python3 -m unittest tests.test_leak_scan -v"""
import base64
import gzip
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import re
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
        tkey = secrets.token_bytes(32)                      # a tenant key (tenant_keys.hkey_hex)
        chain = hmac.new(tkey, b"chain-key:v1", hashlib.sha256).digest()   # wp/keys.py chain_key, sent as hex by keysvc
        cls.broker, cls.key = broker, key
        cls.values = [app, admin, legacy, broker, espw, key, enc, kms.hex(), tkey.hex(), chain.hex()]
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
            # review D F5: the tenant chain key, derived from a tenant key, as the key service sends it and in base64
            "chain_key_hex_in_rpc_log": json.dumps({"op": "chain_key", "ok": chain.hex()}),
            "chain_key_base64": _b64(chain),
            # review D F6: UTF-16 (both byte orders, odd alignment) and percent-encoded JSON unicode escapes
            "utf16le_log": f"broker password {broker}\n".encode("utf-16-le"),
            "utf16be_odd_offset": b"\x07" + f"pw={app}".encode("utf-16-be"),
            "percent_encoded_json_unicode_escapes": urllib.parse.quote(
                '{"api_key": "' + "".join("\\u%04x" % ord(c) for c in key) + '"}', safe=""),
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
        (bindir / "docker").write_text('#!/bin/sh\n[ -e "$FAKE_KS_FAIL" ] && exit 1\n'
                                       'case "$*" in *subject_keys*) cat "$FAKE_KS";; esac\n'
                                       'case "$*" in *tenant_keys*) cat "$FAKE_KS_TENANT";; esac\nexit 0\n')
        (bindir / "docker").chmod(0o755)
        (cls.tmp / "keys.txt").write_text(kms.hex() + "\n" + secrets.token_bytes(32).hex() + "\n")
        (cls.tmp / "tenant_keys.txt").write_text(tkey.hex() + "\n")
        cls.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_KS=str(cls.tmp / "keys.txt"),
                       FAKE_KS_TENANT=str(cls.tmp / "tenant_keys.txt"), FAKE_KS_FAIL=str(cls.tmp / "ks_fail"))
        cls.rc, cls.out = cls.run_scan(home, res)
        cls.hits = {pathlib.Path(ln.split()[1]).name for ln in cls.out.splitlines() if ln.startswith("HIT ")}

    @classmethod
    def run_scan(cls, home, *dirs, env=None, extra=(), timeout=600):
        r = subprocess.run([sys.executable, SCANNER, str(home), *map(str, dirs), *extra], capture_output=True,
                           text=True, env=env or cls.env, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def leaked(self, out):
        """Indexes of the fake values with a 12-character fragment in `out` (never the fragments themselves)."""
        return [i for i, v in enumerate(self.values) if any(v[j:j + 12] in out for j in range(0, len(v) - 11))]

    def fresh(self, name):
        d = self.tmp / name
        d.mkdir()
        (d / "clean.json").write_text(json.dumps({"verdict": "PASS"}))
        return d

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
        self.assertEqual(self.leaked(self.out), [], "the scanner output contains a fragment of a secret value (indexes only shown)")

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

    # ---- review D F4: fail closed, every skip listed
    def test_missing_root_fails_closed(self):
        rc, out = self.run_scan(self.home, self.tmp / "clean", self.tmp / "no_such_results_dir")
        self.assertEqual(rc, 2, "a requested directory that does not exist must make the scan fail closed (exit 2)")

    def test_unreadable_directory_fails_closed(self):
        d = self.fresh("with_locked_dir")
        (d / "locked").mkdir()
        (d / "locked" / "log").write_text("nothing here\n")
        (d / "locked").chmod(0)
        try:
            if os.access(d / "locked", os.R_OK):
                self.skipTest("running as a user who can read a mode-000 directory")
            rc, _ = self.run_scan(self.home, d)
        finally:
            (d / "locked").chmod(0o700)
        self.assertEqual(rc, 2, "a directory the walk could not enter was skipped silently (must exit 2)")

    def test_sibling_of_a_secret_source_is_scanned(self):
        d = self.home.parent / "home" / "oldhome-results"   # shares a string prefix with the oldhome source
        d.mkdir()
        try:
            (d / "log").write_text(f"pw {self.broker}\n")
            rc, _ = self.run_scan(self.home, d)
        finally:
            shutil.rmtree(d)
        self.assertEqual(rc, 1, "a results directory beside a secret source was excluded by a string prefix")

    def test_damaged_gzip_fails_closed(self):
        d = self.fresh("bad_gzip")
        (d / "log.gz").write_bytes(b"\x1f\x8b\x08\x00" + secrets.token_bytes(64))
        rc, _ = self.run_scan(self.home, d)
        self.assertEqual(rc, 2, "a gzip file that cannot be decompressed was skipped silently (must exit 2)")

    def test_truncated_gzip_is_searched_as_far_as_it_decompresses(self):
        d = self.fresh("truncated_gzip")
        z = gzip.compress((f"first line {self.broker}\n" + "filler line\n" * 20000).encode())
        (d / "log.gz").write_bytes(z[:len(z) // 2])
        rc, _ = self.run_scan(self.home, d)
        self.assertEqual(rc, 1, "the readable part of a truncated gzip file was not searched")

    def test_content_over_the_limit_is_never_sliced(self):
        d = self.fresh("oversize")
        (d / "big.gz").write_bytes(gzip.compress(b"\0" * (3 * 1024 * 1024)))
        rc, out = self.run_scan(self.home, d, env=dict(self.env, LEAK_SCAN_MAX_BYTES=str(1024 * 1024)))
        self.assertEqual(rc, 2, "content over the size limit must be listed as not scanned (exit 2), not sliced")

    def test_special_file_is_listed_not_read(self):
        d = self.fresh("fifo")
        os.mkfifo(d / "pipe")
        try:
            rc, _ = self.run_scan(self.home, d, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("the scanner blocked reading a FIFO")
        self.assertEqual(rc, 2, "a file that is not a regular file must be listed as not scanned (exit 2)")

    def test_symlinked_directory_is_never_skipped_silently(self):
        # scp -r copies a linked directory's content. A link inside the scanned roots is searched; one that leaves them
        # is listed and not followed (a link to / must not make the scan unbounded): exit 2.
        d, t = self.fresh("with_link"), self.fresh("link_target")
        (t / "log").write_text(f"pw {self.broker}\n")
        (d / "more").symlink_to(t, target_is_directory=True)
        rc, _ = self.run_scan(self.home, d)
        self.assertEqual(rc, 2, "a symlinked directory outside the scanned roots was skipped silently")
        rc, _ = self.run_scan(self.home, d, t)
        self.assertEqual(rc, 1, "a symlinked directory inside the scanned roots was not searched")

    # ---- review D F12: nothing printed holds a candidate; a name holding one is a hit
    def test_path_name_holding_a_candidate_is_a_hit_and_printed_redacted(self):
        d = self.fresh("named")
        bad = d / f"dump-{self.broker[3:25]}.log"
        bad.write_text("nothing secret inside\n")
        bad.chmod(0)
        try:
            rc, out = self.run_scan(self.home, d)
        finally:
            bad.chmod(0o600)
        self.assertEqual(self.leaked(out), [], "the scanner printed a path holding a secret fragment (indexes only)")
        self.assertEqual(rc, 1, "a file name holding a secret value is copied with the results: it is a hit")

    def test_waiver_reason_is_printed_redacted(self):
        rc, out = self.run_scan(self.home, self.tmp / "clean", extra=("--no-keystore", f"see {self.key}"))
        self.assertEqual(self.leaked(out), [], "the scanner printed a waiver reason holding a secret value")

    # ---- review D F5 and F6: inventory from the creation sites, stated limits
    def test_every_creation_site_is_in_the_inventory_table(self):
        spec = importlib.util.spec_from_file_location("leak_scan_under_test", SCANNER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        table = getattr(mod, "SITES", None)
        self.assertIsNotNone(table, "the scanner has no creation-site table (SITES)")
        pat = re.compile(r"token_(bytes|hex|urlsafe)\(|urandom\(|hmac\.new\(|hmac\.digest\(|SystemRandom|"
                         r"secrets\.choice\(|encrypt_password\(|elasticsearch-(reset-password|service-tokens|"
                         r"create-enrollment-token)|_security/(api_key|service|user)\b.*\b(POST|PUT)\b|"
                         r"\b(POST|PUT)\b.*_security/(api_key|service|user)\b")
        found, unlisted = set(), []
        for top in ("wp", "host", "exp"):
            for f in sorted((HERE / top).rglob("*")):
                if f.suffix not in (".py", ".sh", ".sql") or not f.is_file():
                    continue
                rel = str(f.relative_to(HERE))
                for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if not pat.search(line):
                        continue
                    m = [i for i, s in enumerate(table) if s[0] == rel and s[1] in line]
                    if m:
                        found.update(m)
                    else:
                        unlisted.append(f"{rel}:{n}")
        self.assertEqual(unlisted, [], "secret creation or derivation sites missing from leak_scan.SITES")
        stale = [f"{s[0]} {s[1]!r}" for i, s in enumerate(table) if i not in found]
        self.assertEqual(stale, [], "leak_scan.SITES entries that match no line any more")

    def test_clearance_states_the_split_rule_and_limits(self):
        rc, out = self.run_scan(self.home, self.tmp / "clean")
        self.assertEqual(rc, 0)
        self.assertIn("16 consecutive characters", out, "a clearance must state exactly what it can and cannot find")

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
