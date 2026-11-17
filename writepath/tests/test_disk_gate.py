"""Offline unit test (no stores, no docker): exp/disk_gate.py fails closed (review N2).

docker and du are stubbed; WP_HOME is a temporary home with this stream's conf, so wp/config.py loads without opening
any connection. The gate under test is loaded from a file: this tree's exp/disk_gate.py by default, or the file named
in DISK_GATE_UNDER_TEST (failing-first: the 1fcf3f7 file fails the fail-open cases).
usage (from writepath/): python3 -m unittest discover -s tests -v   (all offline tests; 9 of them here)"""
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-writepath", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-writepath-pg", "es_container": "emlab2-writepath-es"}
GIB = 1024 ** 3


class Host:
    """Stubbed docker/du: sizes in bytes, a log text, return codes, an optional timeout."""

    def __init__(self):
        self.pg, self.es, self.home = 1 * GIB, 1 * GIB, 1 * GIB
        self.log, self.log_rc, self.log_timeout = "INFO started\n", 0, False
        self.du_rc = 0
        self.started = "2000-01-01T00:00:00.000000000Z"

    def run(self, argv, **kw):
        if argv[:2] == ["docker", "logs"]:
            if self.log_timeout:
                raise subprocess.TimeoutExpired(argv, 300)
            return SimpleNamespace(returncode=self.log_rc, stdout=self.log if self.log_rc == 0 else "",
                                   stderr="" if self.log_rc == 0 else "Error response from daemon: No such container")
        if argv[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=self.started + "\n", stderr="")
        if argv[:2] == ["docker", "exec"]:
            size = self.pg if "postgresql" in argv[-1] else self.es
            return SimpleNamespace(returncode=self.du_rc, stdout=f"{size}\t{argv[-1]}\n" if self.du_rc == 0 else "", stderr="")
        if argv[0] == "du":
            return SimpleNamespace(returncode=0, stdout=f"{self.home}\t{argv[-1]}\n", stderr="")
        raise AssertionError(f"unexpected command {argv}")


class DiskGateFailsClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        sys.path.insert(0, str(HERE))
        src = os.environ.get("DISK_GATE_UNDER_TEST") or str(HERE / "exp" / "disk_gate.py")
        spec = importlib.util.spec_from_file_location("disk_gate_under_test", src)
        cls.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.g)
        cls.home = home

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.h = Host()
        # patched for this test only (the gate calls subprocess.run and shutil.disk_usage through those modules)
        for target, attr, stub in ((subprocess, "run", self.h.run),
                                   (shutil, "disk_usage", lambda p: SimpleNamespace(total=100 * GIB, used=70 * GIB, free=30 * GIB))):
            p = mock.patch.object(target, attr, stub)
            p.start()
            self.addCleanup(p.stop)
        stop = self.home / "state" / "STOP"
        if stop.exists():
            stop.unlink()
        self.call("start")

    def call(self, *args):
        """Returns the exit code (0 when main returns) and the printed text."""
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.g.main(*args)
            return 0, buf.getvalue()
        except SystemExit as e:
            return e.code, buf.getvalue() + (str(e.code) if not isinstance(e.code, int) else "")

    def assertVoid(self, rc, out):
        self.assertEqual(rc, 3, f"expected VOID (exit 3), got {rc!r}: {out.strip()[-200:]}")
        self.assertIn("VOID", out)

    # positive controls: a clean run passes, a flood line voids it
    def test_clean_run_passes(self):
        self.assertEqual(self.call("check", "x")[0], 0)
        self.assertEqual(self.call("end")[0], 0)

    def test_flood_line_voids(self):
        self.h.log = "WARN flood stage disk watermark [15gb] exceeded\n"
        self.assertVoid(*self.call("end"))

    # the fail-open cases of review N2
    def test_log_command_failure_voids(self):
        self.h.log_rc = 1
        self.assertVoid(*self.call("end"))

    def test_log_timeout_voids(self):
        self.h.log_timeout = True
        self.assertVoid(*self.call("end"))

    def test_cap_breach_during_last_experiment_voids(self):
        self.assertEqual(self.call("check", "last")[0], 0)
        self.h.es += 3 * GIB                        # grows after the last check
        self.assertVoid(*self.call("end"))

    def test_usage_not_measured_at_end_voids(self):
        self.h.du_rc = 1
        self.assertVoid(*self.call("end"))

    def test_usage_not_measured_at_check_voids(self):
        self.h.du_rc = 1
        self.assertVoid(*self.call("check", "x"))

    def test_stop_written_during_last_experiment_voids(self):
        (self.home / "state" / "STOP").write_text("flood stage seen\n")
        self.assertVoid(*self.call("end"))

    def test_es_container_restarted_during_run_voids(self):
        self.h.started = "2999-01-01T00:00:00.000000000Z"
        self.assertVoid(*self.call("end"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
