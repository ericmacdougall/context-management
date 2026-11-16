"""Collect this round's clean stamped results into results/stamped/ (run on the host) and print an index:
file, code commit, dirty flag, start time. A result is stamped when it has a `provenance` block (experiments, integration
cases) or a `code_commit` (unit reports). Round-one files carry neither and are not collected."""
import glob
import json
import os
import shutil

R = os.path.expanduser("~/emlab-16096/engine/results")
OUT = os.path.join(R, "stamped")
os.makedirs(OUT, exist_ok=True)
for f in sorted(glob.glob(R + "/*.json"), key=os.path.getmtime):
    d = json.load(open(f))
    prov = d.get("provenance")
    code = d.get("code_commit")
    if not (prov or code):
        continue
    dirty = (prov or {}).get("git_dirty") if prov else (code or {}).get("dirty")
    if dirty:      # runs from an uncommitted tree are debugging runs, not evidence
        continue
    shutil.copy(f, OUT)
    if prov:
        print(os.path.basename(f), prov["git_commit"][:7], prov["git_dirty"], prov["started_at"])
    else:
        print(os.path.basename(f), (code or {}).get("commit", "")[:7], (code or {}).get("dirty"), d.get("started_at"))
