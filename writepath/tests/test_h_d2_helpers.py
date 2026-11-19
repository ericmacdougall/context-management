"""exp/h_d2_uids's root helpers under the gate's service-container rule (main's h_d2 decision), offline. Docker is
faked: the test records every `docker run` argv the orchestrator would execute (prepare, witness, the final cleanup,
via main with zero runs and the services stubbed) and checks the helpers' mounts.

  H2H1  the PREP helper is the named container emlab2-wphard-h2prep: WP_HOME/secrets read-only, WP_HOME/svc and
        WP_HOME/run read-write, nothing else; its bind sources exist before docker runs (docker never creates them).
  H2H2  the WITNESS helper is the named container emlab2-wphard-h2witness: WP_HOME/svc and WP_HOME/secrets, every
        mount read-only.
  H2H3  no helper run mounts WP_HOME as a whole, every helper run is named, unnetworked and carries the stream label,
        and the only read-write mounts in the whole experiment's helper runs are WP_HOME/svc and WP_HOME/run.
  H2H4  /h/svc and /h/run are mount points in PREP: they are emptied (find -mindepth 1 -delete), never `rm -rf`ed."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

if not os.environ.get("WP_HOME"):                 # an isolated lab home, as tests/test_store_tmpfs.py does
    _home = pathlib.Path(tempfile.mkdtemp()) / "home"
    (_home / "conf").mkdir(parents=True)
    for _k, _v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                   "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
        (_home / "conf" / _k).write_text(_v + "\n")
    os.environ["WP_HOME"] = str(_home)

from wp import config  # noqa: E402
from exp import h_d2_uids as H  # noqa: E402

PREP, WITNESS = "emlab2-wphard-h2prep", "emlab2-wphard-h2witness"


def _opt(args, name):
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]


def _mounts(args):
    out = []
    for v in _opt(args, "-v"):
        parts = v.split(":")
        out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "rw"))
    return out


class Helpers(unittest.TestCase):
    def setUp(self):
        self.home = config.HOME
        self.secrets = config.SECRETS
        self.secrets.mkdir(parents=True, exist_ok=True)
        for f in ("pgpass.wp_app", "pgpass.wp_verifier", "pgpass.wp_kms", "pgpass", "es_elastic_password"):
            (self.secrets / f).write_text("x\n")
        self.runs = []
        self.existed = []

        def fake_docker(*a, check=True, timeout=600):
            if a and a[0] == "run":
                self.runs.append(list(a))
                self.existed.append({h: os.path.isdir(h) for h, _, _ in _mounts(a)})
            out = ""
            if a and a[0] == "run" and _opt(a, "--name")[:1] == [WITNESS]:
                out = "\n".join(f"{n}=yes" for n in H.PROBE_FILES)
            return mock.Mock(stdout=out, stderr="", returncode=0)
        p = mock.patch.object(H, "_docker", side_effect=fake_docker)
        p.start()
        self.addCleanup(p.stop)

    def helper_runs(self, name):
        return [r for r in self.runs if _opt(r, "--name")[:1] == [name]]

    def test_h2h1_prep_mounts(self):
        H.prepare("fenced")
        runs = self.helper_runs(PREP)
        self.assertEqual(len(runs), 1, self.runs)
        self.assertEqual(sorted(_mounts(runs[0])), sorted([
            (str(self.secrets), "/h/secrets", "ro"), (str(self.home / "svc"), "/h/svc", "rw"),
            (str(self.home / "run"), "/h/run", "rw")]))
        i = self.runs.index(runs[0])
        self.assertTrue(all(self.existed[i].values()), self.existed[i])

    def test_h2h2_witness_read_only(self):
        H.prepare("fenced")
        w = H.witness("fenced")
        self.assertEqual(set(w.values()), {"yes"})
        runs = self.helper_runs(WITNESS)
        self.assertEqual(len(runs), 1, self.runs)
        self.assertEqual(sorted(_mounts(runs[0])), sorted([
            (str(self.home / "svc"), "/h/svc", "ro"), (str(self.secrets), "/h/secrets", "ro")]))

    def test_h2h3_whole_experiment_helper_runs(self):
        stubs = {"start_services": None, "stop_services": None, "once": None}
        with mock.patch.multiple(H, **{k: mock.DEFAULT for k in stubs}), \
                mock.patch("exp.common.write_result"):
            self.assertEqual(H.main(0), 0)
        self.assertTrue(self.runs)
        rw = set()
        for r in self.runs:
            self.assertIn(_opt(r, "--name")[:1], ([PREP], [WITNESS]), r)
            self.assertEqual(_opt(r, "--network"), ["none"], r)
            self.assertIn(H.LABEL, _opt(r, "--label"), r)
            for h, c, mode in _mounts(r):
                self.assertNotEqual(os.path.realpath(h), os.path.realpath(self.home), r)
                self.assertNotEqual(c, "/h", r)
                if mode != "ro":
                    rw.add(h)
        self.assertEqual(rw, {str(self.home / "svc"), str(self.home / "run")})
        # prepare + witness per arm, and the final cleanup
        self.assertEqual(len(self.helper_runs(PREP)), 3, self.runs)
        self.assertEqual(len(self.helper_runs(WITNESS)), 2, self.runs)

    def test_h2h5_d6_uses_the_named_helpers(self):
        # exp/h_d6_broker reuses these helpers for its own credential layout and witness (H6, H6c, H6e)
        from exp import h_d6_broker as D
        D.prepare("fenced")
        D.witness()
        self.assertEqual([_opt(r, "--name")[:1] for r in self.runs], [[PREP], [WITNESS]], self.runs)
        for r in self.runs:
            for h, c, mode in _mounts(r):
                self.assertNotEqual(os.path.realpath(h), os.path.realpath(self.home), r)
                if mode != "ro":
                    self.assertIn(h, (str(self.home / "svc"), str(self.home / "run")), r)
        script = self.runs[0][-1]
        self.assertIn("install -d -m 0777 /h/run/c12", script)     # D6's phase-file directory is still made

    def test_h2h6_every_h2_attribute_the_orchestrators_use_exists(self):
        # 35eee02 removed _root while exp/h_d6_broker.py and seal_uids_e2e.py still called it: a static check, since
        # neither orchestrator's main runs offline
        import re
        for f in ("exp/h_d6_broker.py", "results/failing-first/harness/seal_uids_e2e.py"):
            src = (HERE / f).read_text()
            used = set(re.findall(r"\b(?:H2|_h2\(\))\.([A-Za-z_]\w*)", src))
            self.assertTrue(used, f)
            self.assertEqual(sorted(a for a in used if not hasattr(H, a)), [], f)

    def test_h2h4_mount_points_are_emptied_not_removed(self):
        H.prepare("control")
        script = self.helper_runs(PREP)[0][-1]
        self.assertIn("find /h/svc /h/run -mindepth 1 -delete", script)
        self.assertNotIn("rm -rf /h/svc", script)
        self.assertNotIn("rm -rf /h/run", script)


if __name__ == "__main__":
    unittest.main()
