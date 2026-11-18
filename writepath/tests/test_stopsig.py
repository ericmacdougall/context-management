"""exp/stopsig.py (review D2 of f36766c+5811368): a stop signal runs the orchestrator's cleanup.

  SG1  SIGTERM during the work: `finally:` runs (its marker file is written) and the exit status is 143.
  SG2  a second SIGTERM while that cleanup runs does not interrupt it (the cleanup sleeps 1 s and still finishes).
  SG3  the three live orchestrators install the handler before main(): exp/h_d6_broker.py, exp/h_d2_uids.py and
       results/failing-first/harness/seal_uids_e2e.py."""
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
CHILD = """
import sys, time, pathlib
sys.path.insert(0, sys.argv[1])
from exp import stopsig
stopsig.install()
m = pathlib.Path(sys.argv[2])
try:
    m.with_suffix(".started").write_text("1")
    time.sleep(30)
finally:
    time.sleep(1.0)
    m.write_text("cleanup ran")
"""


class StopSignal(unittest.TestCase):
    def run_child(self, second):
        with tempfile.TemporaryDirectory() as d:
            m = pathlib.Path(d) / "marker"
            p = subprocess.Popen([sys.executable, "-I", "-c", CHILD, str(HERE), str(m)])
            t0 = time.time()
            while not m.with_suffix(".started").exists() and time.time() - t0 < 10:
                time.sleep(0.05)
            p.send_signal(signal.SIGTERM)
            if second:
                time.sleep(0.3)
                p.send_signal(signal.SIGTERM)
            rc = p.wait(timeout=20)
            return rc, m.read_text() if m.exists() else None

    def test_sg1_term_runs_finally(self):
        rc, marker = self.run_child(second=False)
        self.assertEqual(marker, "cleanup ran")
        self.assertEqual(rc, 143)

    def test_sg2_second_term_does_not_interrupt_cleanup(self):
        rc, marker = self.run_child(second=True)
        self.assertEqual(marker, "cleanup ran")
        self.assertEqual(rc, 143)

    def test_sg3_orchestrators_install_it(self):
        for f in ("exp/h_d6_broker.py", "exp/h_d2_uids.py", "results/failing-first/harness/seal_uids_e2e.py"):
            s = (HERE / f).read_text()
            main_at = s.index('if __name__ == "__main__":')
            self.assertIn("stopsig.install()", s[main_at:], f)


if __name__ == "__main__":
    unittest.main()
