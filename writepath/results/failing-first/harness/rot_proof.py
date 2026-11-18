"""Rotation proof for the four role keys after the D6 rerun (GoodQA #16096). Prints booleans and counts only.
Run on the lab host from a pinned tree: cd <src-ba46276> && WP_HOME=... $PY -I <this file> <old-keyid-sha256.txt>
  ids_changed      every current role key id hashes to a value not in the old list (the window 2 ids)
  valid_per_name   for each name wp16096-<role>: the number of keys not invalidated (want 1)
  old_ids_valid    how many of the old ids still belong to a key that is not invalidated (want 0)"""
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())
from wp import config, es_keys  # noqa: E402
from wp.es import ES  # noqa: E402

old = {line.split()[0] for line in open(sys.argv[1]) if line.strip()}
cur = {}
for p in sorted(glob.glob(str(config.SECRETS / "es_api_key*.json"))):
    cur[os.path.basename(p)] = hashlib.sha256(json.load(open(p))["id"].encode()).hexdigest()
es = ES(auth=es_keys._superuser_header())
valid, old_valid = {}, 0
for role in ("projector", "eraser", "verifier", "admin"):
    keys = es.req("GET", "/_security/api_key", name=f"wp16096-{role}", ok=(200, 404))[1].get("api_keys", [])
    valid[role] = sum(1 for k in keys if not k.get("invalidated"))
    old_valid += sum(1 for k in keys if not k.get("invalidated") and hashlib.sha256(k["id"].encode()).hexdigest() in old)
print(json.dumps({"files": len(cur), "ids_changed": all(h not in old for h in cur.values()),
                  "valid_per_name": valid, "old_ids_valid": old_valid}))
