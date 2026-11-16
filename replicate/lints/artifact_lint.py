"""Artifact lint for DESIGN 2.2.8 (THRESHOLDS.md, Lints): checks result JSON files for the fields PROOF-RULES asks
for. Detection is by key-name patterns over the recursive key set (and a few value patterns), stated below; it is a
lint for presence, not a judgement of the experiment.

Fields (rule):
  commit (5)       a key matching commit|git_sha|git_head|sha$|head_sha|revision, or a 7-40 hex value under such a key
  env (6)          es version (version|es_version|build_hash) AND licence (licen[cs]e) AND host load (load|loadavg)
  control (1)      a key containing control|broken|mutant|negative|sensitiv
  witness (2)      a key matching ^n$|^n_|count|witness|trials|runs|examined|items_|rows$
  thresholds (4)   a key containing threshold|prereg|registered|committed|bar$|pass_rule
  per_item (5)     a list of at least 10 dicts anywhere in the file
Scope: every *.json under each owner stream's results directories at the worktree HEAD (git ls-tree; content read with
git show), plus the on-disk owner result folders wp-results-aaa0fec/ and memcore-out/results/.
Controls: a planted artifact with none of the fields must fail all six; a planted complete artifact must pass all six.

usage: python artifact_lint.py <work dir> <out.json>
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict

STREAMS = {  # stream -> (worktree, results path prefixes)
    "behaviour": ("elastic-memory-lab-behaviour", ["behaviour/results/"]),
    "eval": ("elastic-memory-lab-eval", ["eval/results/"]),
    "systems": ("elastic-memory-lab-systems", ["systems/results/"]),
    "engine": ("elastic-memory-lab-engine", ["engine/results/"]),
    "policy": ("elastic-memory-lab-policy", ["policy/results/"]),
    "realsession": ("elastic-memory-lab-realsession", ["realsession/results/"]),
    "writepath": ("elastic-memory-lab-writepath", ["writepath/results/", "writepath/"]),
    "phasef": ("elastic-memory-lab-phasef", ["phasef/results/"]),
    "adapters-fix": ("elastic-memory-lab-adapters-fix", ["memcore/results/"]),
}
DISK = {"writepath (wp-results-aaa0fec, on disk)": "wp-results-aaa0fec", "adapters (memcore-out/results, on disk)": "memcore-out/results"}
P = {
    "commit": re.compile(r"commit|git_sha|git_head|(^|_)sha$|head_sha|revision", re.I),
    "es_version": re.compile(r"(^|_)version$|es_version|build_hash", re.I),
    "licence": re.compile(r"licen[cs]e", re.I),
    "load": re.compile(r"load", re.I),
    "control": re.compile(r"control|broken|mutant|negative|sensitiv", re.I),
    "witness": re.compile(r"^n$|^n_|count|witness|trials|runs|examined|items_|rows$", re.I),
    "thresholds": re.compile(r"threshold|prereg|registered|committed|bar$|pass_rule", re.I),
}
HEX = re.compile(r"^[0-9a-f]{7,40}$")


def walk(o, keys, lists, depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            keys.append((str(k), v))
            walk(v, keys, lists, depth + 1)
    elif isinstance(o, list):
        if len(o) >= 10 and sum(isinstance(x, dict) for x in o) >= 10:
            lists.append(len(o))
        for x in o[:200]:
            walk(x, keys, lists, depth + 1)


def check(obj):
    keys, lists = [], []
    walk(obj, keys, lists)
    names = [k for k, _ in keys]
    has = lambda p: any(P[p].search(k) for k in names)
    commit = has("commit") or any(P["commit"].search(k) and isinstance(v, str) and HEX.match(v) for k, v in keys)
    res = {"commit": commit, "env": has("es_version") and has("licence") and has("load"),
           "env_parts": {"es_version": has("es_version"), "licence": has("licence"), "load": has("load")},
           "control": has("control"), "witness": has("witness"), "thresholds": has("thresholds"), "per_item": bool(lists)}
    res["missing"] = [f for f in ("commit", "env", "control", "witness", "thresholds", "per_item") if not res[f]]
    return res


def git(wt, *a):
    return subprocess.run(["git", "-C", wt] + list(a), capture_output=True, text=True)


def main(work, out):
    files, heads = [], {}
    for s, (wt, pre) in STREAMS.items():
        p = os.path.join(work, wt)
        if not os.path.isdir(p):
            continue
        heads[s] = git(p, "rev-parse", "--short", "HEAD").stdout.strip()
        names = git(p, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        for n in names:
            if n.endswith(".json") and any(n.startswith(x) for x in pre) and "/fixtures/" not in n:
                if pre[-1] == "writepath/" and not n.startswith("writepath/results/") and "result" not in n:
                    continue
                files.append((s, "%s@%s:%s" % (wt, heads[s], n), lambda p=p, n=n: git(p, "show", "HEAD:" + n).stdout))
    for s, d in DISK.items():
        dp = os.path.join(work, d)
        for root, _, fs in os.walk(dp):
            for f in fs:
                if f.endswith(".json"):
                    fp = os.path.join(root, f)
                    files.append((s, os.path.relpath(fp, work), lambda fp=fp: open(fp).read()))
    rows, per = [], defaultdict(Counter)
    for s, ident, rd in files:
        try:
            obj = json.loads(rd())
        except Exception as e:
            rows.append({"stream": s, "file": ident, "parse_error": str(e)[:100]})
            per[s]["unparseable"] += 1
            continue
        c = check(obj)
        rows.append({"stream": s, "file": ident, **c})
        per[s]["files"] += 1
        for m in c["missing"]:
            per[s]["missing_" + m] += 1
        per[s]["complete"] += not c["missing"]
        for part, ok in c["env_parts"].items():  # env is strict (ES + licence + load); parts show which piece is absent
            per[s]["env_has_" + part] += ok
    bad = {"result": 1}
    good = {"git_commit": "0123abc", "env": {"es_version": "9.5.6", "licence": "basic", "loadavg": [1, 1, 1]},
            "control": {"violations": 30, "runs": 200}, "thresholds": {"min_rate": 0.1},
            "rows": [{"i": i, "ok": True} for i in range(12)]}
    ctrl = {"planted_bad_missing": check(bad)["missing"], "planted_good_missing": check(good)["missing"]}
    ctrl["bad_fails_all_six"] = len(ctrl["planted_bad_missing"]) == 6
    ctrl["good_passes_all_six"] = not ctrl["planted_good_missing"]
    res = {"stamp": {"thresholds_commit": "8c3b5e6", "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stream_heads": heads},
           "witness": {"files": len(files), "streams": sorted(set(s for s, _, _ in files))},
           "controls": ctrl, "per_stream": {s: dict(v) for s, v in per.items()}, "files": rows}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "files"}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:3])
