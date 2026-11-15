"""Fetch the canary inputs from t2-dev (read only): the 23 whole-pool low-overlap questions' H3 task files, the low-overlap file (with sha256) and the id lists."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

from . import run_pilot as R

W = os.path.join(R.OUT, "analyst_job")
SSH = ["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), "t2-dev"]
REMOTE = "~/emlab-16096/out"


def sh(cmd):
    return subprocess.run(SSH + [cmd], capture_output=True, check=True).stdout


def fetch_all():
    """Task files for the rest and secondary phases: H1, H2, H3 for the 94 questions and the top-20 H3 files of the 23 low-overlap questions (tar over one ssh per directory; no tool probes)."""
    q94 = json.load(open(os.path.join(W, "q94.json")))
    low = json.load(open(os.path.join(W, "low23.json")))
    jobs = [("analyst_tasks", lv, q94, os.path.join(W, "tasks", lv)) for lv in ("H1", "H2", "H3")] + [("analyst_tasks_top20", "H3", low, os.path.join(W, "tasks_top20", "H3"))]
    for root, lv, qs, dest in jobs:
        os.makedirs(dest, exist_ok=True)
        names = " ".join("%s.json" % q for q in qs)
        tarfile.open(fileobj=io.BytesIO(sh("cd %s/%s/%s && tar -cf - %s" % (REMOTE, root, lv, names)))).extractall(dest)
        print(root, lv, len(os.listdir(dest)))


def main():
    os.makedirs(os.path.join(W, "tasks", "H3"), exist_ok=True)
    raw = sh("cat %s/lowoverlap_pool_h3.json" % REMOTE)
    open(os.path.join(W, "lowoverlap_pool_h3.json"), "wb").write(raw)
    print("lowoverlap_pool_h3.json sha256", hashlib.sha256(raw).hexdigest())
    d = json.loads(raw)
    low, q94 = d["thresholds"]["100"]["qids"], sorted(d["ranks"])
    json.dump(low, open(os.path.join(W, "low23.json"), "w"))
    json.dump(q94, open(os.path.join(W, "q94.json"), "w"))
    names = " ".join("%s.json" % q for q in low)
    tf = tarfile.open(fileobj=io.BytesIO(sh("cd %s/analyst_tasks/H3 && tar -cf - %s" % (REMOTE, names))))
    tf.extractall(os.path.join(W, "tasks", "H3"))
    print("low-overlap", len(low), "questions", len(q94), "task files", len(os.listdir(os.path.join(W, "tasks", "H3"))))
    t = json.load(open(os.path.join(W, "tasks", "H3", low[0] + ".json")))
    print("keys", list(t), "n candidates", len(t["candidates"]), "first round id", t["candidates"][0]["round_id"], "session", t["candidates"][0]["session_id"])


if __name__ == "__main__":
    fetch_all() if sys.argv[1:] == ["all"] else main()
