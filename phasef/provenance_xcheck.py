"""Cross-check the rebuilt rounds against ~/emlab-16096/out/emb/rounds.jsonl on t2-dev for a seeded sample of cited round ids (read only, one ssh per id)."""
import json
import os
import random
import subprocess
import sys

from . import run_pilot as R
from .provenance import load_sessions

facts = [json.loads(l) for l in open(os.path.join(R.OUT, "facts_v3_raw.jsonl"))]
sess = load_sessions(os.path.join(R.OUT, "gate16_s.json"))
sample = random.Random(16096).sample(facts, 8)
ok = 0
for f in sample:
    rid = "%s:%d" % (f["session_id"], f["round"])
    out = subprocess.run(["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), "t2-dev", "grep -m1 -F '\"round_id\": \"%s\"' ~/emlab-16096/out/emb/rounds.jsonl" % rid], capture_output=True, text=True).stdout
    if not out.strip():
        print(rid, "NOT FOUND on host")
        continue
    host = json.loads(out)["text"]
    mine = "\n".join("%s: %s" % (t["role"], t["content"]) for t in sess[f["session_id"]][f["round"]])
    same = host == mine
    ok += same
    print(rid, "identical" if same else "DIFFERENT", len(host), len(mine))
print("identical", ok, "of", len(sample))
