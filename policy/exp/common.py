"""Result files: content-addressed JSON stamped with the code commit, the thresholds commit, parameters, time and host.
A run refuses a dirty tree (so the stamped commit is the code that ran) unless ALLOW_DIRTY=1, which marks the result DEV."""
import hashlib
import json
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, ROOT)


def _git(*a):
    return subprocess.run(["git", "-C", ROOT] + list(a), capture_output=True, text=True).stdout.strip()


def stamp():
    sp = os.path.join(ROOT, "GIT_STAMP.json")          # written by sync.sh when the tree is copied to a host without .git
    if os.path.exists(sp) and not os.path.exists(os.path.join(ROOT, "..", ".git")) and not os.path.exists(os.path.join(ROOT, "..", ".git")):
        g = json.load(open(sp))
        return {"commit": g["commit"], "dirty": g["dirty"], "thresholds_commit": g["thresholds_commit"], "tree_sha256": g.get("tree_sha256"),
                "thresholds_sha256": hashlib.sha256(open(os.path.join(ROOT, "THRESHOLDS.md"), "rb").read()).hexdigest(),
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(), "python": platform.python_version(),
                "load_avg": os.getloadavg(), "argv": " ".join(sys.argv), "model_calls": "none (recording stubs only)", "synced_from_git": True}
    dirty = bool(_git("status", "--porcelain", "--", ".", ":!results"))
    if dirty and os.environ.get("ALLOW_DIRTY") != "1":
        sys.exit("refusing to run a proof on a dirty tree (commit first, or ALLOW_DIRTY=1 for a DEV run)")
    return {"commit": _git("rev-parse", "HEAD"), "dirty": dirty, "thresholds_commit": _git("log", "-1", "--format=%H", "--", "THRESHOLDS.md"),
            "thresholds_sha256": hashlib.sha256(open(os.path.join(ROOT, "THRESHOLDS.md"), "rb").read()).hexdigest(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(), "python": platform.python_version(),
            "load_avg": os.getloadavg(), "argv": " ".join(sys.argv), "model_calls": "none (recording stubs only)"}


def write_result(name, params, data, extra_env=None):
    os.makedirs(RESULTS, exist_ok=True)
    st = stamp()
    if extra_env:
        st["env"] = extra_env
    doc = {"name": name, "stamp": st, "params": params, "result": data}
    raw = json.dumps(doc, indent=1, sort_keys=True, default=str).encode()
    h = hashlib.sha256(raw).hexdigest()
    tag = "DEV." if st["dirty"] else ""
    p = os.path.join(RESULTS, "%s%s.%s.json" % (tag, name, h[:16]))
    with open(p, "wb") as fh:
        fh.write(raw)
    print("RESULT %s verdict=%s sha256=%s file=%s" % (name, data.get("verdict"), h, p))
    return h, p
