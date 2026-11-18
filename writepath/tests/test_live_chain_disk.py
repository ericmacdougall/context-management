"""The lab disk rule in the owner's live chain and the ES node it recreates (amendment H6d, the user's decision:
start with >= 10 GB free, stop at 8 GB). Offline: a temporary HOME, a fake `df` reading its value from a file, a fake
python that sleeps; no lock, container or store of the lab is touched.

  DR1  12 GB free: the chain starts (takes and releases its own lock in the temporary HOME).
  DR2  9 GB free: the chain refuses to start (exit 4) and takes no lock.
  DR3  free space falls to 7 GB while a step runs: the watcher stops the step and the chain exits 6 with $O/STOP,
       long before the step's own 60 s; the lock is released.
  DR4  es_security recreates the node with watermarks low 10gb, high 9gb, flood_stage 8gb, whatever the old container
       carried."""
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
        (h / "emlab2-16096").mkdir()
        venv = h / "emlab2-16096" / "wphard" / "wp" / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nsleep 60\n')   # steps sleep; -c returns
        (venv / "python").chmod(0o755)
        b = h / "bin"
        b.mkdir()
        self.dfval = h / "dfval"
        (b / "df").write_text(f"#!/bin/sh\necho Avail\ncat {self.dfval}\n")
        (b / "df").chmod(0o755)
        for t in ("new", "seal0", "h0"):
            (h / t).mkdir()
            (h / t / "GIT_HEAD").write_text(t + "\n")
        self.env = dict(os.environ, HOME=str(h), PATH=f"{b}:{os.environ['PATH']}")

    def tearDown(self):
        self.tmp.cleanup()

    def run_chain(self, kib, steps, wait=None, drop_to=None):
        self.dfval.write_text(f"{kib}\n")
        h = self.home
        p = subprocess.Popen(["sh", str(CHAIN), str(h / "new"), str(h / "seal0"), str(h / "h0"), str(h / "out"), steps],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if drop_to is not None:
            time.sleep(wait)
            self.dfval.write_text(f"{drop_to}\n")
        t0 = time.time()
        out = p.communicate(timeout=120)[0]
        return p.returncode, out, time.time() - t0

    def test_dr1_starts_at_12_gb(self):
        rc, out, _ = self.run_chain(12 * G, "RS0")       # an unknown step name: nothing runs, the chain completes
        self.assertIn("lock taken", out)
        self.assertIn("lock released", out)
        self.assertFalse((self.home / "emlab2-16096" / "RUNLOCK").exists())

    def test_dr2_refuses_at_9_gb(self):
        rc, out, _ = self.run_chain(9 * G, "RS0")
        self.assertEqual(rc, 4, out)
        self.assertIn("below start line", out)
        self.assertFalse((self.home / "emlab2-16096" / "RUNLOCK").exists())

    def test_dr3_stops_at_8_gb_during_a_step(self):
        rc, out, took = self.run_chain(12 * G, "G1", wait=3, drop_to=7 * G)
        self.assertEqual(rc, 6, out)
        self.assertIn("STOPPED during G1", out)
        self.assertLess(took, 40)
        self.assertIn("below stop line", (self.home / "out" / "STOP").read_text())
        self.assertFalse((self.home / "emlab2-16096" / "RUNLOCK").exists())


class NodeWatermarks(unittest.TestCase):
    def test_dr4_recreated_node_watermarks(self):
        if not os.environ.get("WP_HOME"):
            home = pathlib.Path(tempfile.mkdtemp()) / "home"
            (home / "conf").mkdir(parents=True)
            for k, v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                         "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
                (home / "conf" / k).write_text(v + "\n")
            os.environ["WP_HOME"] = str(home)
        sys.path.insert(0, str(HERE))
        from wp import es_security
        j = {"HostConfig": {"PortBindings": {"9200/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19460"}]}},
             "Config": {"Image": "elasticsearch:9.5.6", "Labels": {},
                        "Env": ["cluster.routing.allocation.disk.watermark.low=18gb",
                                "cluster.routing.allocation.disk.watermark.high=17gb",
                                "cluster.routing.allocation.disk.watermark.flood_stage=15gb",
                                "discovery.type=single-node"]}}
        args = es_security._run_args(j, True)
        env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        wm = sorted(e for e in env if e.startswith("cluster.routing.allocation.disk.watermark."))
        self.assertEqual(wm, ["cluster.routing.allocation.disk.watermark.flood_stage=8gb",
                              "cluster.routing.allocation.disk.watermark.high=9gb",
                              "cluster.routing.allocation.disk.watermark.low=10gb"])
        self.assertIn("discovery.type=single-node", env)


if __name__ == "__main__":
    unittest.main()
