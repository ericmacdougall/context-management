"""What es_keys.create() reports, and its rotation (offline; a fake cluster, a temporary secrets directory).

  K-OUT1  the value create() returns (printed by `wp.es_security enable` and `python -m wp.es_keys create`) contains no
          value the stream's leak scan treats as secret: runkit/leak_scan.py over that printed line reports no hit.
          Live origin: 2026-11-18 window at 27094c9, D6.out line 1 printed the four role key ids; the leak scan matched
          them against the "id" fields of secrets/es_api_key*.json (classified by field name, no value shown).
  K-OUT2  each value is a 16-hex sha256 fingerprint, stable for one id, distinct across ids.
  K-ROT   create() invalidates the previous key of a role (DELETE by name) before it mints that role's successor."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

if not os.environ.get("WP_HOME"):                 # an isolated lab home, as tests/test_sealer.py does
    _home = pathlib.Path(tempfile.mkdtemp()) / "home"
    (_home / "conf").mkdir(parents=True)
    for _k, _v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                   "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
        (_home / "conf" / _k).write_text(_v + "\n")
    os.environ["WP_HOME"] = str(_home)

from wp import config, es_keys  # noqa: E402


class FakeES:
    calls = []
    n = 0

    def __init__(self, *a, **k):
        self.auth = None

    def req(self, method, path, body=None, ok=(200, 201), **params):
        FakeES.calls.append((method, path, (body or {}).get("name")))
        if method == "POST":
            FakeES.n += 1
            return 200, {"id": f"KEYID{FakeES.n:04d}abcdefghijklmnop", "encoded": f"ENCODED{FakeES.n:04d}qrstuvwxyz0123456789"}
        return 200, {"invalidated_api_keys": []}


class CreateOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / "secrets").mkdir(mode=0o700)
        self._old = (es_keys.ES, config.SECRETS, es_keys._superuser_header)
        es_keys.ES = FakeES
        config.SECRETS = self.home / "secrets"
        es_keys._superuser_header = lambda: "Basic x"
        FakeES.calls, FakeES.n = [], 0

    def tearDown(self):
        es_keys.ES, config.SECRETS, es_keys._superuser_header = self._old
        self.tmp.cleanup()

    def test_k_out1_printed_line_is_scan_clean(self):
        out = es_keys.create()
        d = self.home / "out"
        d.mkdir()
        (d / "D6.out").write_text(f"security on; key id fingerprints {out}\n")
        files = sorted(p.name for p in (self.home / "secrets").iterdir())
        self.assertEqual(files, ["es_api_key.eraser.json", "es_api_key.json", "es_api_key.projector.json",
                                 "es_api_key.verifier.json"])
        r = subprocess.run([sys.executable, "-I", str(HERE / "runkit" / "leak_scan.py"), str(self.home), str(d)],
                           capture_output=True, text=True, check=True)
        self.assertIn("files with a secret value: 0", r.stdout)
        self.assertNotIn("HIT", r.stdout)

    def test_k_out2_fingerprints(self):
        out = es_keys.create()
        self.assertEqual(sorted(out), ["admin", "eraser", "projector", "verifier"])
        for v in out.values():
            self.assertRegex(v, r"^sha256:[0-9a-f]{16}$")
        self.assertEqual(len(set(out.values())), 4)
        self.assertEqual(es_keys.fingerprint("abc"), es_keys.fingerprint("abc"))

    def test_k_rot_previous_key_invalidated_first(self):
        es_keys.create()
        for role in ("projector", "eraser", "verifier", "admin"):
            name = f"wp16096-{role}"
            seq = [m for m, _, nm in FakeES.calls if nm == name]
            self.assertEqual(seq, ["DELETE", "POST"], role)


if __name__ == "__main__":
    unittest.main()
