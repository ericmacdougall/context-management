"""Before results leave the lab host: does any result or log file contain a secret value of this stream?
Reads candidate secret values from this stream's own 0600 files (WP_HOME/secrets*) in memory only and
prints FILE NAMES and counts, never a value or a fragment of one. usage: python3 leak_scan.py WP_HOME DIR [DIR ...]"""
import base64
import json
import os
import sys

home = sys.argv[1]
values = set()
for sub in os.listdir(home):
    if not sub.startswith("secrets"):
        continue
    for root, _, files in os.walk(os.path.join(home, sub)):
        for f in files:
            p = os.path.join(root, f)
            try:
                raw = open(p, "rb").read()
            except OSError:
                continue
            try:                                   # JSON key files ({"id", "encoded"}): each string value is a candidate
                stack = [json.loads(raw)]
            except ValueError:
                stack = []
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)
                elif isinstance(o, str) and len(o) >= 12 and " " not in o:
                    values.add(o.encode())
                    values.add(base64.b64encode(o.encode()))
            for line in raw.splitlines() + [raw.strip()]:
                line = line.strip()
                cands = [line] + ([line.rsplit(b":", 1)[-1]] if line.count(b":") >= 4 else [])   # pgpass: last field
                for c in cands:
                    if len(c) >= 12 and b" " not in c:
                        values.add(c)
                        values.add(base64.b64encode(c))
print("candidate secret values loaded:", len(values) // 2)
hits, scanned = {}, 0
for d in sys.argv[2:]:
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            scanned += 1
            n = sum(1 for v in values if v in data)
            if n:
                hits[p] = n
print("files scanned:", scanned, "files with a secret value:", len(hits))
for p, n in sorted(hits.items()):
    print("HIT", p, n)
