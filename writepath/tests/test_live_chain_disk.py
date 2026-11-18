"""The lab disk rule in the owner's live chain and the ES node it recreates (amendment H6d, the user's decision:
start with >= 10 GB free, stop at 8 GB; review D1 to D3 of f36766c+5811368). Offline: a temporary HOME, a fake `df`
reading its value from a file, a fake `docker` printing a fake ES log, a fake python whose steps sleep; no lock,
container or store of the lab is touched.

  DR1  12 GB free: the chain starts (takes and releases its own lock in the temporary HOME).
  DR2  9 GB free: the chain refuses to start (exit 4) and takes no lock.
  DR2b the exact boundary: 10485760 KiB starts, 10485759 KiB refuses (red on the 20 GB chain).
  DR3  free space falls to 7 GB during a single-command step: the step is stopped, exit 6, $O/STOP, lock released.
  DR5  the same during the FIRST command of a grouped step (D6): no later command of the group runs (no "exit=" line,
       no h_d6_broker run), its output is renamed D6.out.STOPPED and no D6 summary line is printed (red on 5811368).
  DR6  amendment 23a: a flood-stage line in the ES log during a step makes that step VOID (exit 7, *.VOID, no summary).
  DR7  RP without ROT_OLD rotates inside the window: hash the minted ids (0600), prove, rotate, prove (review J M8 on
       tmpfs stores, where a recreated node keeps no earlier keys).
  DR4  es_security recreates the node with watermarks low 10gb, high 9gb, flood_stage 8gb, whatever the old container
       carried (run in a child process: no WP_HOME, sys.path or module state leaks into this test process)."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
CHAIN = HERE / "results" / "failing-first" / "harness" / "live_chain.sh"
G = 1048576


@unittest.skipUnless(shutil.which("timeout") and shutil.which("pgrep"), "needs timeout and pgrep")
class ChainDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = self.home = pathlib.Path(self.tmp.name)
        wp = h / "emlab2-16096" / "wphard" / "wp"
        (wp / "venv" / "bin").mkdir(parents=True)
        (wp / "conf").mkdir()
        (wp / "conf" / "es_container").write_text("emlab2-wphard-es\n")
        self.calls = h / "calls"
        # every step command sleeps 60 s and records its arguments; `-c` snippets return at once
        (wp / "venv" / "bin" / "python").write_text(
            f'#!/bin/sh\necho "$*" >> {self.calls}\n[ "$1" = "-c" ] && exit 0\nsleep 60\n')
        (wp / "venv" / "bin" / "python").chmod(0o755)
        b = h / "bin"
        b.mkdir()
        self.dfval, self.eslog = h / "dfval", h / "eslog"
        self.eslog.write_text("")
        (b / "df").write_text(f"#!/bin/sh\necho Avail\ncat {self.dfval}\n")
        (b / "docker").write_text(f"#!/bin/sh\n[ \"$1\" = logs ] && cat {self.eslog}\nexit 0\n")
        for f in ("df", "docker"):
            (b / f).chmod(0o755)
        for t in ("new", "seal0", "h0"):
            (h / t).mkdir()
            (h / t / "GIT_HEAD").write_text(t + "\n")
        self.env = dict(os.environ, HOME=str(h), PATH=f"{b}:{os.environ['PATH']}")

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self):
        return (self.home / "emlab2-16096" / "RUNLOCK").exists()

    def run_chain(self, kib, steps, wait=None, drop_to=None, log_line=None):
        self.dfval.write_text(f"{kib}\n")
        h = self.home
        p = subprocess.Popen(["sh", str(CHAIN), str(h / "new"), str(h / "seal0"), str(h / "h0"), str(h / "out"), steps],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t0 = time.time()
        if wait is not None:
            time.sleep(wait)
            if drop_to is not None:
                self.dfval.write_text(f"{drop_to}\n")
            if log_line is not None:
                self.eslog.write_text(log_line + "\n")
        out = p.communicate(timeout=180)[0]
        return p.returncode, out, time.time() - t0

    def test_dr1_starts_at_12_gb(self):
        rc, out, _ = self.run_chain(12 * G, "ZZ")       # an unknown step: nothing runs, the chain completes
        self.assertIn("lock taken", out)
        self.assertIn("lock released", out)
        self.assertFalse(self.lock())

    def test_dr2_refuses_at_9_gb(self):
        rc, out, _ = self.run_chain(9 * G, "ZZ")
        self.assertEqual(rc, 4, out)
        self.assertIn("below start line", out)
        self.assertFalse(self.lock())

    def test_dr2b_exact_boundary(self):
        rc, out, _ = self.run_chain(10485760, "ZZ")
        self.assertIn("lock taken", out)
        self.assertNotEqual(rc, 4, out)
        rc, out, _ = self.run_chain(10485759, "ZZ")
        self.assertEqual(rc, 4, out)
        self.assertFalse(self.lock())

    def test_dr3_stops_at_8_gb_during_a_step(self):
        rc, out, took = self.run_chain(12 * G, "G1", wait=3, drop_to=7 * G)
        self.assertEqual(rc, 6, out)
        self.assertIn("STOPPED during G1", out)
        self.assertLess(took, 40)
        self.assertIn("below stop line", (self.home / "out" / "STOP").read_text())
        self.assertFalse(self.lock())

    def test_dr5_grouped_step_runs_nothing_after_the_stop(self):
        rc, out, took = self.run_chain(12 * G, "D6", wait=3, drop_to=7 * G)
        self.assertEqual(rc, 6, out)
        self.assertIn("STOPPED during D6", out)
        self.assertLess(took, 40, out)
        calls = self.calls.read_text() if self.calls.exists() else ""
        self.assertNotIn("exp.h_d6_broker", calls)
        o = self.home / "out"
        self.assertFalse((o / "D6.out").exists())
        self.assertNotIn("exit=", (o / "D6.out.STOPPED").read_text())
        self.assertNotIn("\nD6 ", "\n" + out)
        self.assertFalse(self.lock())

    def test_dr6_watermark_line_voids_the_step(self):
        rc, out, _ = self.run_chain(12 * G, "G1", wait=2,
                                    log_line='{"log.level":"WARN","message":"flood stage disk watermark [8gb] exceeded on [n]"}')
        self.assertEqual(rc, 7, out)
        self.assertIn("VOID G1", out)
        self.assertTrue((self.home / "out" / "G1.out.VOID").exists())
        self.assertNotIn("G1 exit=", out)
        self.assertFalse(self.lock())


    def test_dr7_rp_rotates_inside_the_window(self):
        # review J M8 on tmpfs stores (a recreated node holds no earlier window's keys): without ROT_OLD the RP step
        # hashes the ids D6 minted (0600), proves, rotates (wp.es_keys create), proves again, in that order
        py = self.home / "emlab2-16096" / "wphard" / "wp" / "venv" / "bin" / "python"
        py.write_text(f'#!/bin/sh\necho "$*" >> {self.calls}\n[ "$1" = "-c" ] && {{ echo aaaa; exit 0; }}\necho "{{}}"\n')
        self.env.pop("ROT_OLD", None)
        rc, out, _ = self.run_chain(12 * G, "RP")
        self.assertEqual(rc, 0, out)
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        at = lambda pred: next((i for i, ln in enumerate(lines) if pred(ln)), None)  # noqa: E731
        h = at(lambda ln: "es_api_key*.json" in ln)           # the hashing snippet (its lines follow "-c")
        p1 = at(lambda ln: "rot_proof.py" in ln and "rot-old.sha256" in ln)
        c = at(lambda ln: ln == "-m wp.es_keys create")
        p2 = next((i for i, ln in enumerate(lines) if "rot_proof.py" in ln and i != p1), None)
        self.assertTrue(None not in (h, p1, c, p2) and h < p1 < c < p2, f"{(h, p1, c, p2)} in {lines}")
        f = self.home / "out" / "rot-old.sha256"
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertIn("RP in-window old ids hashed 1", out)
        self.assertFalse(self.lock())


class NodeWatermarks(unittest.TestCase):
    def test_dr4_recreated_node_watermarks(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "home"
            (home / "conf").mkdir(parents=True)
            for k, v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                         "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
                (home / "conf" / k).write_text(v + "\n")
            code = """
import json
from wp import es_security
j = {"HostConfig": {"PortBindings": {"9200/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19460"}]}},
     "Config": {"Image": "elasticsearch:9.5.6", "Labels": {},
                "Env": ["cluster.routing.allocation.disk.watermark.low=18gb",
                        "cluster.routing.allocation.disk.watermark.high=17gb",
                        "cluster.routing.allocation.disk.watermark.flood_stage=15gb", "discovery.type=single-node"]}}
a = es_security._run_args(j, True)
print(json.dumps([a[i + 1] for i, x in enumerate(a) if x == "-e"]))
"""
            r = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True,
                               env=dict(os.environ, WP_HOME=str(home)))
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            env = json.loads(r.stdout.strip().splitlines()[-1])
        wm = sorted(e for e in env if e.startswith("cluster.routing.allocation.disk.watermark."))
        self.assertEqual(wm, ["cluster.routing.allocation.disk.watermark.flood_stage=8gb",
                              "cluster.routing.allocation.disk.watermark.high=9gb",
                              "cluster.routing.allocation.disk.watermark.low=10gb"])
        self.assertIn("discovery.type=single-node", env)


if __name__ == "__main__":
    unittest.main()
