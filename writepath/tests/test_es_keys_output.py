"""What es_keys.create() reports, and its rotation (offline; a fake cluster, a temporary secrets directory).

  K-OUT1  the value create() returns (printed by `wp.es_security enable` and `python -m wp.es_keys create`) contains no
          value the stream's leak scan treats as secret: runkit/leak_scan.py over that printed line reports no hit.
          Live origin: 2026-11-18 window at 27094c9, D6.out line 1 printed the four role key ids; the leak scan matched
          them against the "id" fields of secrets/es_api_key*.json (classified by field name, no value shown).
  K-OUT2  each value is a 16-hex sha256 fingerprint, stable for one id, distinct across ids.
  K-ROT1  (review J M8) create() mints a role's successor and writes its file BEFORE invalidating the role's other
          keys: after a rotation exactly the file's key is valid for each name, and rotation reports 1 invalidated.
  K-ROT2  a mint that fails leaves the previous key valid and its file unchanged (no outage window).
  K-ROT3  an invalidation that fails leaves two valid keys and reports the error; never none."""
import inspect
import json
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
    """A cluster's API-key store: POST mints, GET ?name=&active_only= lists, DELETE {name} or {ids} invalidates.
    fail_post / fail_delete name the key names whose POST raises / whose DELETE answers 500."""
    calls = []
    n = 0
    keys = {}                      # id -> {"name", "invalidated"}
    fail_post, fail_delete = set(), set()

    def __init__(self, *a, **k):
        self.auth = None

    def req(self, method, path, body=None, ok=(200, 201), **params):
        name = (body or {}).get("name") or params.get("name")
        FakeES.calls.append((method, path, name))
        if method == "POST":
            if name in FakeES.fail_post:
                raise OSError("simulated failure before the mint")
            FakeES.n += 1
            kid = f"KEYID{FakeES.n:04d}abcdefghijklmnop"
            FakeES.keys[kid] = {"name": name, "invalidated": False}
            return 200, {"id": kid, "encoded": f"ENCODED{FakeES.n:04d}qrstuvwxyz0123456789"}
        if method == "GET":
            return 200, {"api_keys": [{"id": i, "name": k["name"], "invalidated": k["invalidated"]} for i, k in FakeES.keys.items()
                                      if k["name"] == name and not (params.get("active_only") == "true" and k["invalidated"])]}
        if method == "DELETE":
            ids = (body or {}).get("ids") or [i for i, k in FakeES.keys.items() if k["name"] == name]
            if any(FakeES.keys[i]["name"] in FakeES.fail_delete for i in ids if i in FakeES.keys):
                return 500, {}
            for i in ids:
                FakeES.keys[i]["invalidated"] = True
            FakeES.calls[-1] = (method, path, tuple(sorted({FakeES.keys[i]["name"] for i in ids})) if ids else name)
            return 200, {"invalidated_api_keys": ids}
        return 200, {}

    @staticmethod
    def active(name):
        return [i for i, k in FakeES.keys.items() if k["name"] == name and not k["invalidated"]]


class CreateOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / "secrets").mkdir(mode=0o700)
        self._old = (es_keys.ES, config.SECRETS, es_keys._superuser_header)
        es_keys.ES = FakeES
        config.SECRETS = self.home / "secrets"
        es_keys._superuser_header = lambda: "Basic x"
        FakeES.calls, FakeES.n, FakeES.keys, FakeES.fail_post, FakeES.fail_delete = [], 0, {}, set(), set()

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

    ROLES = ("projector", "eraser", "verifier", "admin")

    @staticmethod
    def create(rot):
        """create(rot) where the tree reports rotation; an older tree runs without it (red by assertion)."""
        if "rotation" in inspect.signature(es_keys.create).parameters:
            return es_keys.create(rot)
        return es_keys.create()

    def file_id(self, role):
        f = "es_api_key.json" if role == "admin" else f"es_api_key.{role}.json"
        return json.loads((self.home / "secrets" / f).read_text())["id"]

    def test_k_rot1_mint_first_then_invalidate_the_others(self):
        es_keys.create()                                   # window 1
        first = {r: self.file_id(r) for r in self.ROLES}
        FakeES.calls = []
        rot = {}
        self.create(rot)                                   # window 2: rotation
        for role in self.ROLES:
            name = f"wp16096-{role}"
            seq = [m for m, _, nm in FakeES.calls if nm == name or (isinstance(nm, tuple) and name in nm)]
            self.assertEqual(seq[0], "POST", f"{role}: something ran before the successor was minted: {seq}")
            self.assertEqual(FakeES.active(name), [self.file_id(role)], f"{role}: not exactly the file's key valid")
            self.assertNotEqual(self.file_id(role), first[role])
            self.assertEqual(rot.get(role), [1, None], role)

    def test_k_rot2_failed_mint_leaves_the_old_key(self):
        es_keys.create()
        old = self.file_id("projector")
        FakeES.fail_post = {"wp16096-projector"}
        with self.assertRaises(OSError):
            es_keys.create()
        self.assertEqual(FakeES.active("wp16096-projector"), [old], "a failed mint left the role without its key")
        self.assertEqual(self.file_id("projector"), old)

    def test_k_rot3_failed_invalidation_leaves_two_never_none(self):
        es_keys.create()
        FakeES.fail_delete = {"wp16096-eraser"}
        rot = {}
        self.create(rot)
        self.assertIn(self.file_id("eraser"), FakeES.active("wp16096-eraser"))
        self.assertEqual(len(FakeES.active("wp16096-eraser")), 2)
        self.assertEqual(rot.get("eraser"), [0, "invalidate 500"])


if __name__ == "__main__":
    unittest.main()
