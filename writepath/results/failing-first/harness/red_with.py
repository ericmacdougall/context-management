"""Failing-first runner: copy writepath/ to a scratch dir, replace ONE file with an older version (a commit) or a broken
variant (@<file>), run one test module there, print a provenance header, the unittest summary and failure headlines.
usage (from writepath/): python3 results/failing-first/harness/red_with.py <writepath dir> <path under writepath/>
                         <commit | @variant file> <test module>
Exit 0 means the replaced code FAILED the tests (the red we want); exit 1 means it passed."""
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from prov import header  # noqa: E402

wp, rel, src, mod = sys.argv[1:5]
wp = pathlib.Path(wp).resolve()
if src.startswith("@"):
    old = pathlib.Path(src[1:]).read_text()
    what = f"broken variant {pathlib.Path(src[1:]).name} (sha256 {hashlib.sha256(old.encode()).hexdigest()[:16]})"
else:
    old = subprocess.run(["git", "-C", str(wp), "show", f"{src}:./{rel}"], capture_output=True, text=True, check=True).stdout
    what = f"{rel} at {src}"
d = pathlib.Path(tempfile.mkdtemp()) / "writepath"
shutil.copytree(wp, d, ignore=shutil.ignore_patterns("results", "__pycache__"))
(d / rel).write_text(old)
r = subprocess.run([sys.executable, "-m", "unittest", mod], cwd=d, capture_output=True, text=True)
shutil.rmtree(d.parent)
print("\n".join(header(wp, __file__)))
print(f"# replaced: wp file {rel} <- {what}; tests: current; python3 -m unittest {mod}")
for ln in r.stderr.splitlines():
    if ln.startswith(("FAIL:", "ERROR:", "Ran ", "OK", "FAILED")) or "Error:" in ln:
        print(ln.replace(str(d), "<copy>"))
sys.exit(0 if r.returncode else 1)
