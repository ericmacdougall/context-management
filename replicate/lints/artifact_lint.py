"""Artifact lint for DESIGN 2.2.8 (THRESHOLDS.md, Lints): checks result JSON files for the fields PROOF-RULES asks
for. Detection is by key-name patterns over the recursive key set (and a few value patterns), stated below; it is a
lint for presence, not a judgement of the experiment.

Fields (rule), matched on lower-cased keys as whole '_' tokens; the first matching key path is recorded per file:
  commit (5)       commit/sha/head/revision key (not model_*) whose value is a hex SHA (optionally -dirty) or holds '@<sha>'
  env (6)          ES version (build_hash, es_version, or a semver version/number under an es/elastic* key) AND a
                   licence key AND a load-average key (load_avg/loadavg; not workload, payload, load_s)
  control (1)      control(s)/broken/mutant/negative/sensitiv*/ablation token
  witness (2)      n, n_*, runs, trials, samples, scenarios, questions, items, rows, witness*, examined
  thresholds (4)   threshold(s)/prereg*/bar(s)/pass_rule/criteria/acceptance token, or a key exactly 'registered'
  per_item (5)     a list of at least 10 dicts anywhere in the file
Scope: every *.json under each owner stream's results directories at the worktree HEAD (git ls-tree; content read with
git show), plus the on-disk owner result folders wp-results-aaa0fec/, memcore-out/results/ and phasef-out/ (top level only).
Streams whose worktree holds no result JSON at HEAD are listed with files 0.
Controls: a planted empty artifact must fail all six; a planted near-miss (workload, committed, def_version,
fold_count, payload_check, a_late_commit) must fail commit, env and thresholds; a planted complete one must pass all six.
Run 1 (06457a3) used substring patterns and over-matched (workload as load, committed as commit); superseded.

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
DISK = {"writepath (wp-results-aaa0fec, on disk)": ("wp-results-aaa0fec", True), "adapters (memcore-out/results, on disk)": ("memcore-out/results", True),
        "phasef (phasef-out top level, on disk)": ("phasef-out", False)}   # phasef-out: top-level files only (cache/tasks are inputs)
P = {  # matched on lower-cased keys as whole '_'-separated tokens (run 1 used substrings: 'workload' passed as load)
    "commit": re.compile(r"(^|_)(git_)?(commit|sha|head|revision|rev)(_sha|_id)?$|^(sut|script|code)_commit|_commit_|^commit"),
    "licence": re.compile(r"(^|_)licen[cs]e(_|$)"),
    "load": re.compile(r"(^|_)load_?(avg|average)(_|$)"),  # not load_s (a duration)
    "control": re.compile(r"(^|_)(controls?|broken|mutants?|negative|sensitiv\w*|ablation)(_|$)"),
    "witness": re.compile(r"^(n|n_\w+|witness\w*|trials|runs|examined|samples?|scenarios|questions|items|rows)$|(^|_)(witness\w*|trials|examined)(_|$)"),
    "thresholds": re.compile(r"(^|_)(thresholds?|prereg\w*|bars?|pass_rule|criteria|acceptance)(_|$)|^registered$"),
}
HEX = re.compile(r"^[0-9a-f]{7,40}(-dirty)?$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+")


def walk(o, keys, lists, path=(), depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            keys.append((str(k).lower(), v, path))
            walk(v, keys, lists, path + (str(k).lower(),), depth + 1)
    elif isinstance(o, list):
        if len(o) >= 10 and sum(isinstance(x, dict) for x in o) >= 10:
            lists.append(len(o))
        for x in o[:200]:
            walk(x, keys, lists, path, depth + 1)


def first(keys, pred):
    for k, v, path in keys:
        if pred(k, v, path):
            return "/".join(path + (k,))
    return None


def check(obj):
    keys, lists = [], []
    walk(obj, keys, lists)
    m = {f: first(keys, lambda k, v, p, f=f: bool(P[f].search(k))) for f in ("licence", "load", "control", "witness", "thresholds")}
    # commit: a commit-like key whose value is a hex SHA (optionally -dirty), or a string containing '@<hex>'
    m["commit"] = first(keys, lambda k, v, p: not k.startswith("model_") and P["commit"].search(k) and isinstance(v, str) and (HEX.match(v.strip()) or re.search(r"[@ ][0-9a-f]{7,40}\b", v)))
    # ES version: a semver value under es_version/version/number with an elastic/es ancestor, or a build_hash key
    m["es_version"] = first(keys, lambda k, v, p: k in ("es_version", "elasticsearch_version", "build_hash") or (
        k in ("version", "number") and isinstance(v, str) and SEMVER.match(v) and any(re.search(r"(^|_)(es|elastic\w*)(_|$)", a) for a in p)))
    res = {"commit": bool(m["commit"]), "env": bool(m["es_version"] and m["licence"] and m["load"]),
           "env_parts": {"es_version": bool(m["es_version"]), "licence": bool(m["licence"]), "load": bool(m["load"])},
           "control": bool(m["control"]), "witness": bool(m["witness"]), "thresholds": bool(m["thresholds"]), "per_item": bool(lists),
           "matched_keys": m}
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
    for s, (d, rec) in DISK.items():
        dp = os.path.join(work, d)
        for root, dirs, fs in os.walk(dp):
            if not rec:
                dirs[:] = []
            for f in fs:
                if f.endswith(".json"):
                    fp = os.path.join(root, f)
                    files.append((s, os.path.relpath(fp, work), lambda fp=fp: open(fp).read()))
    rows, per = [], defaultdict(Counter)
    for s in list(STREAMS) + list(DISK):
        per[s]["files"] += 0
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
    good = {"git_commit": "0123abc", "env": {"elasticsearch": {"version": "9.5.6"}, "licence": "basic", "loadavg": [1, 1, 1]},
            "control": {"violations": 30, "runs": 200}, "thresholds": {"min_rate": 0.1},
            "rows": [{"i": i, "ok": True} for i in range(12)]}
    near = {"workload": "x", "committed": 3, "def_version": 2, "basis_version": 1, "fold_count": 1, "payload_check": {},
            "a_late_commit": "rejected", "runs_with_stale_commit": 4}
    ctrl = {"planted_bad_missing": check(bad)["missing"], "planted_good_missing": check(good)["missing"],
            "planted_near_miss_missing": check(near)["missing"]}
    ctrl["near_miss_fails_commit_env_thresholds"] = all(f in ctrl["planted_near_miss_missing"] for f in ("commit", "env", "thresholds"))
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
