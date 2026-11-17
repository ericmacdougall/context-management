"""Provenance header shared by the failing-first harnesses (review A6): which tree, which command, which harness."""
import hashlib
import pathlib
import subprocess
import sys


def header(writepath, harness_file):
    wp = pathlib.Path(writepath).resolve()
    git = ["git", "-C", str(wp)]
    head = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(git + ["status", "--porcelain", "--", "."], capture_output=True, text=True).stdout.strip()
    h = hashlib.sha256(pathlib.Path(harness_file).read_bytes()).hexdigest()[:16]
    name = pathlib.Path(harness_file).name
    args = " ".join(sys.argv[1:]).replace(str(wp), "<writepath>")
    return [f"# tree: writepath/ at {head}{' (dirty: ' + str(len(dirty.splitlines())) + ' paths)' if dirty else ' (clean)'}",
            f"# command: python3 results/failing-first/harness/{name} {args}",
            f"# harness sha256: {h}"]
