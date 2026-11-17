"""Offline unit test (no stores, no docker): exp/disk_gate.py voids a run on each doubt it is meant to catch (review N2,
step-1 delta review P1 and P2).

docker and du are stubbed; WP_HOME is a temporary home with this stream's conf, so wp/config.py loads without opening
any connection. The gate under test is loaded from a file: this tree's exp/disk_gate.py by default, or the file named
in DISK_GATE_UNDER_TEST (failing-first: the 1fcf3f7 file fails the fail-open cases).
usage (from writepath/): python3 -m unittest tests.test_disk_gate -v"""
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
GIB = 1024 ** 3


class Host:
    """Stubbed docker/du: sizes in bytes, a log text (the lines ES writes during the run), return codes, an optional
    timeout. The container log is assembled as: lines before the run, the gate's start marker, self.log, the gate's
    end marker; each line carries a driver timestamp. Faults (review P1): rotated (everything up to and including the
    start marker gone), truncated (the file emptied mid-run: only lines after a point remain), a replacement container
    (new id), a restart (StartedAt differs in the nanoseconds only), an end marker that never lands."""

    def __init__(self):
        self.pg, self.es, self.home = 1 * GIB, 1 * GIB, 1 * GIB
        self.log, self.log_rc, self.log_timeout = "INFO started\n", 0, False
        self.du_rc = 0
        self.cid = "c1dba572fdb8" + "0" * 52
        self.started = "2026-11-17T17:20:44.846939281Z"
        self.logcfg = '{"Config":{},"Type":"json-file"}'
        self.markers = []
        self.rotated = self.truncated = self.end_lost = self.pre_flood = False
        self.on_du = None                           # called when the gate measures WP_HOME (P2: a STOP arriving mid-end)

    def _log_rows(self):
        rows = [("before", "INFO an earlier line")]
        if self.pre_flood:
            rows.append(("before", "WARN flood stage disk watermark [15gb] exceeded (before this run)"))
        start = [m for m in self.markers if " start " in m]
        end = [m for m in self.markers if " end " in m]
        rows += [("start", m) for m in start]
        rows += [("run", ln) for ln in self.log.splitlines() if ln]
        if not self.end_lost:
            rows += [("end", m) for m in end]
        if self.rotated:
            rows = [r for r in rows if r[0] in ("run", "end")]
        if self.truncated:
            rows = [r for r in rows if r[0] == "end"] or [("after", "INFO after truncation")]
        out, n = [], 0
        for _, txt in rows:
            n += 1
            out.append(f"2026-11-17T18:00:{n:02d}.000000000Z {txt}")
        return "\n".join(out) + "\n"

    def run(self, argv, **kw):
        if argv[:2] == ["docker", "logs"]:
            if self.log_timeout:
                raise subprocess.TimeoutExpired(argv, 300)
            return SimpleNamespace(returncode=self.log_rc, stdout=self._log_rows() if self.log_rc == 0 else "",
                                   stderr="" if self.log_rc == 0 else "Error response from daemon: No such container")
        if argv[:2] == ["docker", "inspect"]:
            fmt = argv[argv.index("-f") + 1] if "-f" in argv else ""
            out = self.started if fmt == "{{.State.StartedAt}}" else f"{self.cid}|{self.started}|{self.logcfg}"   # older gates ask for StartedAt only
            return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
        if argv[:2] == ["docker", "exec"] and argv[3:5] == ["sh", "-c"]:
            line = argv[5].split(">")[0].replace("echo", "", 1).strip()
            self.markers.append(line)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["docker", "exec"]:
            size = self.pg if "postgresql" in argv[-1] else self.es
            return SimpleNamespace(returncode=self.du_rc, stdout=f"{size}\t{argv[-1]}\n" if self.du_rc == 0 else "", stderr="")
        if argv[0] == "du":
            if self.on_du:
                self.on_du()
            return SimpleNamespace(returncode=0, stdout=f"{self.home}\t{argv[-1]}\n", stderr="")
        raise AssertionError(f"unexpected command {argv}")


class DiskGateVoids(unittest.TestCase):
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
        if hasattr(self.g, "_sleep"):
            p = mock.patch.object(self.g, "_sleep", lambda s: None)
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

    # amendment 23a: the real line ES logged at 15:54:43Z during d3 at 31112b4 (node id shortened); fails on 31112b4
    def test_high_watermark_line_voids(self):
        self.h.log = ('{"log.level": "WARN","message":"high disk watermark [17gb] exceeded on [node][7b751d48f8c8]'
                      '[/usr/share/elasticsearch/data] free: 15.7gb[2.4%], shards will be relocated away from this node"}\n')
        self.assertVoid(*self.call("end"))

    def test_low_watermark_line_does_not_void(self):
        self.h.log = ('{"log.level": "INFO","message":"low disk watermark [18gb] exceeded on [node][7b751d48f8c8]'
                      '[/usr/share/elasticsearch/data] free: 17.7gb[2.7%], replicas will not be assigned to this node"}\n')
        self.assertEqual(self.call("end")[0], 0)

    # amendment 24: the start line is 20 GB host free (above the ES high watermark, 17gb); fails on 1bad3bd (17 GB)
    def _start_with_free(self, gib):
        with mock.patch.object(shutil, "disk_usage",
                               lambda p: SimpleNamespace(total=100 * GIB, used=(100 - gib) * GIB, free=gib * GIB)):
            return self.call("start")

    def test_start_under_20gb_blocked(self):
        rc, out = self._start_with_free(19)
        self.assertEqual(rc, 2, f"expected BLOCKED (exit 2) at 19 GiB free, got {rc!r}: {out.strip()[-200:]}")
        self.assertIn("BLOCKED", out)

    def test_start_at_20gb_allowed(self):
        self.assertEqual(self._start_with_free(20)[0], 0)

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

    def test_stop_reason_names_the_watcher_not_an_operator(self):
        # amendment H4: the flood watcher's STOP was reported as "operator stop"
        (self.home / "state" / "STOP").write_text("flood watcher: flood stage or high watermark in the ES log at 10:00:00\n")
        rc, out = self.call("end")
        self.assertVoid(rc, out)
        self.assertIn("flood watcher", out)
        self.assertNotIn("operator stop", out)

    def test_empty_stop_file_is_not_attributed(self):
        (self.home / "state" / "STOP").write_text("")
        rc, out = self.call("check", "x")
        self.assertVoid(rc, out)
        self.assertIn("writer unknown", out)
        self.assertNotIn("operator stop", out)

    # review P1 (step-1 delta review at 3c2864f): log coverage must be established, not assumed
    def test_p1_flood_line_before_the_run_does_not_void(self):
        self.h.pre_flood = True
        self.assertEqual(self.call("end")[0], 0, "the stub's pre-run flood line is outside the window")

    def test_p1_rotated_logs_void(self):
        self.h.rotated = True                       # rotation dropped everything up to the start marker
        self.assertVoid(*self.call("end"))

    def test_p1_truncated_logs_void(self):
        self.h.log = "WARN flood stage disk watermark [15gb] exceeded\n"
        self.h.truncated = True                     # the log file was emptied mid-run, the flood line with it
        self.assertVoid(*self.call("end"))

    def test_p1_same_second_restart_voids(self):
        self.h.started = self.h.started[:20] + "999999999Z"     # same second, different nanoseconds
        self.assertVoid(*self.call("end"))

    def test_p1_different_container_id_voids(self):
        self.h.cid = "d" * 64                       # a replacement container with the same name
        self.assertVoid(*self.call("end"))

    def test_p1_clock_step_back_voids(self):
        real = time.time
        with mock.patch.object(time, "time", lambda: real() - 3600):
            self.assertVoid(*self.call("end"))

    def test_p1_end_marker_never_lands_voids(self):
        self.h.end_lost = True
        self.assertVoid(*self.call("end"))

    # review P2: a STOP is never lost
    def test_p2_start_with_stop_present_is_blocked_and_keeps_it(self):
        stop = self.home / "state" / "STOP"
        stop.write_text("flood watcher: flood stage at 10:00:00\n")   # arrived after the chain looked, before start
        rc, out = self.call("start")
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)
        self.assertTrue(stop.exists(), "start deleted a STOP")

    def test_p2_stop_arriving_while_end_measures_voids(self):
        stop = self.home / "state" / "STOP"
        self.h.on_du = lambda: stop.write_text("operator: stop now\n")
        self.assertVoid(*self.call("end"))

    def test_p2_clear_stop_consumes_with_a_recorded_reason(self):
        stop = self.home / "state" / "STOP"
        stop.write_text("flood watcher: flood stage at 10:00:00\n")
        rc, out = self.call("clear-stop", "read it; the flood was from another tenant's run, which ended")
        self.assertEqual(rc, 0, out)
        self.assertFalse(stop.exists())
        log = (self.home / "state" / "stop_clears.log").read_text()
        self.assertIn("flood watcher", log)
        self.assertIn("another tenant", log)
        self.assertEqual(self.call("start")[0], 0)

    def test_p2_clear_stop_needs_a_reason(self):
        (self.home / "state" / "STOP").write_text("x\n")
        rc, _ = self.call("clear-stop", "  ")
        self.assertNotEqual(rc, 0)
        self.assertTrue((self.home / "state" / "STOP").exists())

    def test_es_container_restarted_during_run_voids(self):
        self.h.started = "2999-01-01T00:00:00.000000000Z"
        self.assertVoid(*self.call("end"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
