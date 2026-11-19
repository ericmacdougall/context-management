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
        # rot_proof answers like the live one (window A2): before = ids unchanged, all 4 old valid; after = rotated
        n = self.home / "n"
        py.write_text(f'#!/bin/sh\necho "$*" >> {self.calls}\n[ "$1" = "-c" ] && {{ echo aaaa; exit 0; }}\n'
                      f'case "$*" in *rot_proof*) echo x >> {n}\n'
                      f'  if [ "$(wc -l < {n})" -eq 1 ]; then echo \'{BEFORE}\'; else echo \'{AFTER}\'; fi;;\n'
                      '  *) echo "{}";; esac\n')
        self.env.pop("ROT_OLD", None)
        rc, out, _ = self.run_chain(12 * G, "RP")
        self.assertEqual(rc, 0, out)
        self.assertIn("RP:pass/pass", out)
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

    # Declared outcomes (main, after window A at 1ffbfa1 exited 0 although D6 exited 1): the chain exits 3 whenever a
    # step ends otherwise than declared, and its last lines print expected/observed per step.
    def fake(self, rc, text="", c_rc=0):
        """python whose step commands print `text` and exit `rc`; `-c` snippets exit `c_rc`."""
        py = self.home / "emlab2-16096" / "wphard" / "wp" / "venv" / "bin" / "python"
        (self.home / "pyout").write_text(text)
        py.write_text(f'#!/bin/sh\necho "$*" >> {self.calls}\n[ "$1" = "-c" ] && exit {c_rc}\n'
                      f'cat {self.home / "pyout"}\nexit {rc}\n')

    def signature(self, step, entries):
        """entries: "<FAIL|ERROR> <test>" lines, as in results/failing-first/harness/expected-red/."""
        d = self.home / "new" / "results" / "failing-first" / "harness" / "expected-red"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{step}.txt").write_text(f"# {step} test signature\n" + "".join(n + "\n" for n in entries))

    @staticmethod
    def unittest_out(failed, passed=(), errors=()):
        lines = [f"{t} (tests.test_d3_pg.D3Store.{t}) ... FAIL" for t in failed]
        lines += [f"{t} (tests.test_d3_pg.D3Store.{t}) ... ERROR" for t in errors]
        lines += [f"{t} (tests.test_d3_pg.D3Store.{t}) ... ok" for t in passed]
        lines += ["", *(f"ERROR: {t} (tests.test_d3_pg.D3Store.{t})" for t in errors),
                  *(f"FAIL: {t} (tests.test_d3_pg.D3Store.{t})" for t in failed), "",
                  f"Ran {len(failed) + len(passed) + len(errors)} tests in 0.1s", "",
                  f"FAILED (failures={len(failed)}, errors={len(errors)})" if failed or errors else "OK"]
        return "\n".join(lines) + "\n"

    def test_dr8_failing_pass_step_fails_the_chain(self):
        # D6 as in window A at 1ffbfa1: es_security and the licence check fine, h_d6_broker exits 1
        self.fake(1, 'h_d6_broker {"fenced_holds": "0/5"}\n')
        rc, out, _ = self.run_chain(12 * G, "D6")
        self.assertEqual(rc, 3, out)
        self.assertIn("D6:pass/fail(exit=1)", out)
        self.assertIn("CHAIN FAILED", out)
        self.assertFalse(self.lock())

    def test_dr8b_failing_unittest_step_fails_the_chain(self):
        self.fake(1, self.unittest_out(["test_a"]))
        rc, out, _ = self.run_chain(12 * G, "G1")
        self.assertEqual(rc, 3, out)
        self.assertIn("G1:pass/fail(G1 exit=1)", out)

    def test_dr9_expected_red_that_goes_green_fails_the_chain(self):
        self.signature("XB", ["FAIL test_n1a", "FAIL test_s10"])
        self.env["XB_TREE"] = str(self.home / "new")
        self.fake(0, self.unittest_out([], ["test_n1a", "test_s10"]))
        rc, out, _ = self.run_chain(12 * G, "XB")
        self.assertEqual(rc, 3, out)
        self.assertIn("XB:red/green", out)

    def test_dr10_expected_red_with_its_signature_passes(self):
        self.signature("XB", ["FAIL test_n1a", "ERROR test_s10"])
        self.env["XB_TREE"] = str(self.home / "new")
        self.fake(1, self.unittest_out(["test_n1a"], ["test_ok"], errors=["test_s10"]))
        rc, out, _ = self.run_chain(12 * G, "XB")
        self.assertEqual(rc, 0, out)
        self.assertIn("XB:red/red", out)
        self.assertIn("CHAIN OK", out)

    def test_dr11_red_for_another_reason_fails_the_chain(self):
        # red, but one named test passed and another failed instead (the failure moved): not the declared red
        self.signature("RS", ["FAIL test_b5", "FAIL test_b6"])
        self.fake(1, self.unittest_out(["test_b5", "test_other"], ["test_b6"]))
        rc, out, _ = self.run_chain(12 * G, "RS")
        self.assertEqual(rc, 3, out)
        self.assertIn("RS:red/red-without-signature(missing 1 extra 1)", out)

    def test_dr11b_an_extra_failure_fails_the_chain(self):
        self.signature("RS", ["FAIL test_b5", "FAIL test_b6"])
        self.fake(1, self.unittest_out(["test_b5", "test_b6", "test_c1"]))
        rc, out, _ = self.run_chain(12 * G, "RS")
        self.assertEqual(rc, 3, out)
        self.assertIn("RS:red/red-without-signature(missing 0 extra 1)", out)

    def test_dr11c_the_kind_is_pinned(self):
        # the signature says the test errors (e.g. a missing function); a plain assertion failure is another red
        self.signature("RH", ["ERROR test_b4", "FAIL test_b5"])
        self.fake(1, self.unittest_out(["test_b4", "test_b5"]))
        rc, out, _ = self.run_chain(12 * G, "RH")
        self.assertEqual(rc, 3, out)
        self.assertIn("RH:red/red-without-signature(missing 1 extra 1)", out)

    def test_dr11d_names_are_matched_whole(self):
        # test_n1a must not be satisfied by test_n1a2 failing
        self.signature("RS", ["FAIL test_n1a"])
        self.fake(1, self.unittest_out(["test_n1a2"], ["test_n1a"]))
        rc, out, _ = self.run_chain(12 * G, "RS")
        self.assertEqual(rc, 3, out)
        self.assertIn("RS:red/red-without-signature(missing 1 extra 1)", out)

    def test_dr11e_the_committed_signatures_parse(self):
        # the repo's own expected-red files, replayed as unittest output of their kinds: XB, RS and RH judge red/red
        src = HERE / "results" / "failing-first" / "harness" / "expected-red"
        dst = self.home / "new" / "results" / "failing-first" / "harness" / "expected-red"
        shutil.copytree(src, dst)
        self.env["XB_TREE"] = str(self.home / "new")
        out_all = ""
        for st in ("XB", "RS", "RH"):
            ent = [ln.split() for ln in (src / f"{st}.txt").read_text().splitlines() if ln and not ln.startswith("#")]
            self.assertTrue(ent and all(k in ("FAIL", "ERROR") and t.startswith("test_") for k, t in ent), st)
            self.fake(1, self.unittest_out([t for k, t in ent if k == "FAIL"], ["test_ok"],
                                           errors=[t for k, t in ent if k == "ERROR"]))
            rc, out, _ = self.run_chain(12 * G, st)
            self.assertEqual(rc, 0, out)
            out_all += out
        for st in ("XB", "RS", "RH"):
            self.assertIn(f"{st}:red/red", out_all)

    def test_dr12_red_step_without_signature_file_fails_the_chain(self):
        self.fake(1, self.unittest_out(["test_b5"]))
        rc, out, _ = self.run_chain(12 * G, "RH")
        self.assertEqual(rc, 3, out)
        self.assertIn("RH:red/fail(no signature file)", out)

    def test_dr13_mixed_chain_prints_every_step_and_fails_on_one(self):
        self.fake(0, self.unittest_out([], ["test_a"]))
        rc, out, _ = self.run_chain(12 * G, "G1 G3 ZZ")
        self.assertEqual(rc, 3, out)
        self.assertIn("steps expected/observed: G1:pass/pass G3:pass/pass ZZ:pass/fail(unknown step)", out)

    def test_dr15_verdict_fail_with_exit_0_fails_the_chain(self):
        # exp/registry_marker and exp/d4_store_mark exit 0 whatever their verdict
        self.fake(0, "registry_marker FAIL fenced 9 / 10 controls violated {}\n")
        rc, out, _ = self.run_chain(12 * G, "P1")
        self.assertEqual(rc, 3, out)
        self.assertIn("P1:pass/fail(verdict not PASS)", out)
        self.fake(0, "d4_store_mark {'pass': False, 'why': ['x']}\n")
        rc, out, _ = self.run_chain(12 * G, "P2")
        self.assertEqual(rc, 3, out)
        self.assertIn("P2:pass/fail(verdict not pass)", out)
        self.fake(0, "registry_marker PASS fenced 10 / 10\nd4_store_mark {'pass': True, 'why': []}\n")
        rc, out, _ = self.run_chain(12 * G, "P1 P2")
        self.assertEqual(rc, 0, out)
        self.assertIn("P1:pass/pass P2:pass/pass", out)

    def test_dr14_all_steps_as_declared(self):
        self.fake(0, self.unittest_out([], ["test_a"]))
        rc, out, _ = self.run_chain(12 * G, "G1 G3")
        self.assertEqual(rc, 0, out)
        self.assertIn("steps expected/observed: G1:pass/pass G3:pass/pass", out)
        self.assertIn("CHAIN OK", out)


BEFORE = ('{"files": 1, "ids_changed": false, "valid_per_name": {"projector": 1, "eraser": 1, "verifier": 1, '
          '"admin": 1}, "old_ids_valid": 1}')
AFTER = ('{"files": 1, "ids_changed": true, "valid_per_name": {"projector": 1, "eraser": 1, "verifier": 1, '
         '"admin": 1}, "old_ids_valid": 0}')


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
