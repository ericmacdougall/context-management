"""Mutation evidence for memcore/reprobe.py (goodqa#16096 deploy part 3). Run from the repo root:

    python3 -m memcore.tests.mutation_check_reprobe

For each mutation it rewrites memcore/reprobe.py in place, runs the named tests, restores the file (always), and requires the
tests to FAIL BY ASSERTION: unittest reports FAIL (an assertion), not ERROR (an exception, import failure or hang). Prints one
line per mutation and exits 1 if any mutation survives or errors. The unmutated file must pass first.
Committed output: memcore/tests/mutation_check_reprobe.out."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(ROOT, "memcore", "reprobe.py")
R = "memcore.tests.test_reprobe."
H = "memcore.tests.test_reprobe_hardening."

MUTATIONS = [
    ("M1 licensed tier never sets strategy auto", 'self.search.strategy = "auto"', "pass",
     [R + "ReprobeFollowsTheTier.test_restart_while_node_unreachable_then_licensed_leaves_the_basic_path_within_the_backoff"]),
    ("M2 adapter latch never cleared on return", "                        reset()", "                        pass",
     [R + "ReprobeFollowsTheTier.test_licence_returns_after_a_403_latched_the_adapter_and_the_next_call_uses_the_backend"]),
    ("M3 a failing refresh re-raises (loop ends)", "            return None\n\n    def _retry_delay_for_publish", "            raise\n\n    def _retry_delay_for_publish",
     [H + "Hardening.test_an_exception_inside_a_refresh_is_recorded_and_the_next_refresh_runs"]),
    ("M4 backoff exponent not capped", "exp = min(max(self.failures - 1, 0), _MAX_EXPONENT)", "exp = max(self.failures - 1, 0)",
     [H + "Hardening.test_thousands_of_consecutive_failures_never_end_the_loop_or_overflow_the_delay"]),
    ("M5 a failed publication is marked published", '            self.last_error = "publish:" + type(e).__name__\n',
     '            self.last_error = "publish:" + type(e).__name__\n            self._published = self._key(rep)\n',
     [H + "Hardening.test_a_failed_publication_is_retried_without_another_tier_change"]),
    ("M6 nan, inf, negative and zero intervals accepted", 'or not math.isfinite(v) or v <= 0:', 'or False:',
     [H + "Hardening.test_the_tracker_refuses_unusable_intervals"]),
    ("M7 a refresh finishing after stop() still applies", "            if self._stop.is_set():\n                return None", "            if False:\n                return None",
     [H + "Hardening.test_a_refresh_that_finishes_after_stop_applies_nothing"]),
    ("M8 latch cleared on every licensed answer", "(before != \"enterprise\" or not was_auto)", "True",
     [H + "Hardening.test_an_unchanged_enterprise_answer_leaves_the_adapter_latch_alone"]),
]


def run(tests):
    p = subprocess.run([sys.executable, "-m", "unittest", "-v"] + tests, cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = p.stderr + p.stdout
    return p.returncode, len(re.findall(r"\.\.\. FAIL$", out, re.M)), len(re.findall(r"\.\.\. ERROR$", out, re.M)), out


def main():
    original = open(TARGET).read()
    allt = sorted({t for m in MUTATIONS for t in m[3]})
    rc, nf, ne, out = run(allt)
    print("unmutated: rc=%d failures=%d errors=%d (%d tests)" % (rc, nf, ne, len(allt)))
    if rc != 0:
        print(out[-2000:])
        return 1
    bad = 0
    try:
        for name, old, new, tests in MUTATIONS:
            if original.count(old) != 1:
                print("%-60s ANCHOR NOT FOUND (%d)" % (name, original.count(old)))
                bad += 1
                continue
            open(TARGET, "w").write(original.replace(old, new))
            rc, nf, ne, out = run(tests)
            ok = rc != 0 and nf >= 1 and ne == 0
            print("%-60s rc=%d failures=%d errors=%d  %s" % (name, rc, nf, ne, "DETECTED BY ASSERTION" if ok else "NOT OK"))
            bad += 0 if ok else 1
    finally:
        open(TARGET, "w").write(original)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
