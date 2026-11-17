"""Failing-first for tests/test_broker.py (D6): copy writepath/ to a scratch dir, apply ONE mutation to wp/broker.py at a
time, run the broker test, and report the result. Each mutant must fail at least one test (exit 1 if any survives).
usage (from writepath/): python3 results/failing-first/harness/broker_mutants.py <writepath dir>"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from prov import header  # noqa: E402

MUTANTS = {   # name: (exact text in wp/broker.py, replacement)
    "no_recheck_after_cas": ('        if not self.lease_ok(t, L.PROJECTOR, epoch, holder):\n            raise Refused("lease_not_current")              # lost',
                             '        if False:\n            raise Refused("lease_not_current")              # lost'),
    "writer_gets_manage": ('"privileges": ["create_doc", "index"]}', '"privileges": ["create_doc", "index", "manage"]}'),
    "invalidate_any_tenant": ('if (k.get("metadata") or {}).get("tenant") == t and ', 'if '),
    "mint_before_cas": ('        F.fence_es(self.es, t, epoch)                      # broker-held CAS',
                        '        self._mint("x", {}, {})\n        F.fence_es(self.es, t, epoch)                      # broker-held CAS'),
}
wp = pathlib.Path(sys.argv[1]).resolve()
print("\n".join(header(wp, __file__)))
rc = 0
for name, (old, new) in MUTANTS.items():
    d = pathlib.Path(tempfile.mkdtemp()) / "writepath"
    shutil.copytree(wp, d, ignore=shutil.ignore_patterns("results", "__pycache__"))
    p = d / "wp" / "broker.py"
    s = p.read_text()
    assert s.count(old) == 1, f"{name}: anchor not found exactly once"
    p.write_text(s.replace(old, new))
    r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_broker"], cwd=d, capture_output=True, text=True)
    last = [ln for ln in r.stderr.splitlines() if ln.startswith(("FAILED", "OK", "Ran"))]
    fails = sorted({ln.split(" ")[1] for ln in r.stderr.splitlines() if ln.startswith(("FAIL:", "ERROR:"))})
    print(f"{name}: exit={r.returncode} {' | '.join(last)} | failing: {', '.join(fails) or '-'}")
    rc |= (r.returncode == 0)
    shutil.rmtree(d.parent)
sys.exit(rc)
