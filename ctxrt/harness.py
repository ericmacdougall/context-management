"""Proof runner plumbing: stamp (commit, time, params, fingerprint), refuse a dirty tree, hashed per-case JSONL, summary JSON."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "ctxrt", "results")
SEEDS = [16096001, 16096002, 16096003, 16096004, 16096005]
THRESHOLD_COMMITS = ["67a2842", "05aafb0"]


def _git(*a):
    return subprocess.check_output(["git", "-C", ROOT] + list(a), text=True).strip()


def stamp(params: dict, allow_dirty: bool = False) -> dict:
    dirty = _git("status", "--porcelain", "--", ".", ":!ctxrt/results")
    if dirty and not allow_dirty:
        raise SystemExit("refusing to run on a dirty tree:\n" + dirty)
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    return {"code_commit": _git("rev-parse", "HEAD"), "dirty": bool(dirty), "threshold_commits": THRESHOLD_COMMITS,
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": params, "python": sys.version.split()[0],
            "host": platform.node(), "machine": platform.platform(), "loadavg": load, "timing_quiesced": False,
            "fingerprint": {"es": "none (in-process stubs)", "model": "deterministic stubs, 0 model calls", "stores": "in-memory + sqlite"}}


def write_cases(name: str, seed: int, rows) -> dict:
    os.makedirs(RESULTS, exist_ok=True)
    p = os.path.join(RESULTS, "%s-%d.jsonl" % (name, seed))
    h = hashlib.sha256()
    with open(p, "w") as fh:
        for r in rows:
            line = json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            fh.write(line)
            h.update(line.encode())
    return {"path": os.path.relpath(p, ROOT), "sha256": h.hexdigest(), "n": len(rows)}


def write_summary(name: str, doc: dict) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    p = os.path.join(RESULTS, "%s.json" % name)
    with open(p, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    return p
