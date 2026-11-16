"""Fetch the stage-G inputs from t2-dev (read only, one tar per directory) and extract the 77 part-B questions from the dataset on this Mac."""
import io
import json
import os
import subprocess
import sys
import tarfile

from . import data, run_pilot as R

G = os.path.join(R.OUT, "stage_g")
SSH = ["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), "t2-dev"]
REMOTE = "~/emlab-16096/out"


def sh(cmd):
    return subprocess.run(SSH + [cmd], capture_output=True, check=True).stdout


def untar(cmd, dest):
    os.makedirs(dest, exist_ok=True)
    tarfile.open(fileobj=io.BytesIO(sh(cmd))).extractall(dest)


def main():
    untar("cd %s/analyst_tasks_g && tar -cf - H3" % REMOTE, os.path.join(G, "A"))
    untar("cd %s/stage_g && COPYFILE_DISABLE=1 tar -cf - B" % REMOTE, G)
    ids = json.load(open(os.path.join(G, "B", "question_ids.json")))
    print("B question_ids.json type", type(ids).__name__, list(ids)[:5] if isinstance(ids, dict) else ids[:3])
    print("A task files", len(os.listdir(os.path.join(G, "A", "H3"))), {a: len(os.listdir(os.path.join(G, "B", a))) for a in ("RRF", "ANALYST", "ORACLE")})


if __name__ == "__main__":
    main()
