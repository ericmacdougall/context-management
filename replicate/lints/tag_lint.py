"""Tag lint for DESIGN 2.1.4 (THRESHOLDS.md, Lints). Mechanical; no judgement about the science.

Per DESIGN table row with an evidence tag:
 (i)   the tag is in the closed set;
 (ii)  every artifact file named in the cite (*.json, *.md, *.py, *.jsonl) exists in the repository the cite's source
       key points at, at the commit the cite names (else the source's default commit from the Sources table), checked
       with `git ls-tree -r <sha>`; research/ files are checked on disk;
 (iii) LEDGER consistency: LEDGER rows are matched to DESIGN rows by shared distinctive tokens (experiment ids such as
       F8, E9b, X04, file stems, commits); a matched pair whose LEDGER class and DESIGN tag disagree in kind is reported.
A planted DESIGN row citing a nonexistent artifact must be flagged (control); DESIGN section 6 items 10 and 11 are known
positives the LEDGER check should rediscover (reported either way).

usage: python tag_lint.py <work dir> <out.json>
"""
import json
import os
import re
import subprocess
import sys
import time

TAGS = {"REVIEWED", "OWNER-REPORTED", "OBSERVED", "EXPLORATORY", "DESIGNED-UNTESTED", "DID-NOT-HOLD", "OPEN-DECISION"}
# source key -> (worktree dir, default commit or None for HEAD-of-lab-main, path hints)
SRC = {
    "SYS": ("elastic-memory-lab-systems", "a13b524"), "BEH": ("elastic-memory-lab-behaviour", "5229e75"),
    "PF": ("elastic-memory-lab-phasef", "27a4c64"), "LR": ("elastic-memory-lab-links-rescue", "18d07a3"),
    "PREREG": ("elastic-memory-lab-eval", "5d39de1"), "XP": ("elastic-memory-lab-eval", "5d39de1"),
    "CURVE": ("elastic-memory-lab-eval", "5d39de1"), "RAGCTX": ("elastic-memory-lab-eval", "5d39de1"),
    "CANARY": ("elastic-memory-lab-eval", "5d39de1"), "AFULL": ("elastic-memory-lab-eval", "cc83d26"),
    "ENG": ("elastic-memory-lab-engine", "62916ae"), "WP": ("elastic-memory-lab-writepath", "aaa0fec"),
    "MC": ("elastic-memory-lab-adapters-fix", "c20e208"), "GOT": ("elastic-memory-lab", "603bb3b"),
    "LEDGER": ("elastic-memory-lab", "603bb3b"),
}
WORKTREE_OF_BRANCH = {
    "feat/systems-skeleton": "elastic-memory-lab-systems", "feat/elastic-behaviour": "elastic-memory-lab-behaviour",
    "feat/phasef-harness": "elastic-memory-lab-phasef", "feat/links-rescue": "elastic-memory-lab-links-rescue",
    "feat/eval-scaffold": "elastic-memory-lab-eval", "feat/engine-evolution": "elastic-memory-lab-engine",
    "feat/writepath-rescue": "elastic-memory-lab-writepath", "fix/adapters-delta-16096": "elastic-memory-lab-adapters-fix",
    "feat/ports-adapters": "elastic-memory-lab-adapters", "feat/policy-decisions": "elastic-memory-lab-policy",
}
FILE = re.compile(r"[A-Za-z0-9_.\-/]+\.(?:json|jsonl|md|py)\b")
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
ROW = re.compile(r"^\|\s*(\d+(?:\.\d+)+)\s*\|(.*)\|\s*$")


def git(wt, *args):
    r = subprocess.run(["git", "-C", wt] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout


_tree_cache = {}


def tree(wt, sha):
    k = (wt, sha)
    if k not in _tree_cache:
        rc, out = git(wt, "ls-tree", "-r", "--name-only", sha)
        _tree_cache[k] = set(out.split()) if rc == 0 else None
    return _tree_cache[k]


def find_in_tree(files, name):
    if files is None:
        return None
    base = os.path.basename(name)
    return [f for f in files if f == name or f.endswith("/" + name) or os.path.basename(f) == base]


def parse_design(text):
    rows = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        ev = cells[-1]
        t = re.match(r"\[([A-Z\-]+)\]", ev)
        rows.append({"id": m.group(1), "statement": " | ".join(cells[:-1])[:300], "evidence": ev, "tag": t.group(1) if t else None})
    return rows


def check_row(row, work):
    ev = row["evidence"]
    problems, checked = [], []
    if row["tag"] not in TAGS:
        problems.append("tag not in closed set: %r" % row["tag"])
    keys = [k for k in SRC if re.search(r"\b%s\b" % re.escape(k), ev)]
    branches = [b for b in WORKTREE_OF_BRANCH if b in ev]
    shas = SHA.findall(ev)
    for f in sorted(set(FILE.findall(ev))):
        if f.startswith("research/"):
            ok = os.path.exists(os.path.join(work, f))
            checked.append({"file": f, "where": "work dir", "exists": ok})
            if not ok:
                problems.append("missing research file %s" % f)
            continue
        cands = []
        for b in branches:
            wt = WORKTREE_OF_BRANCH[b]
            for s in (shas or [None]):
                cands.append((wt, s or "HEAD"))
        for k in keys:
            wt, d = SRC[k]
            for s in (shas or [d]):
                cands.append((wt, s))
        if f in ("DESIGN.md", "LEDGER.md", "PROOF-RULES.md") or f.startswith("gotchas/"):
            cands.append(("elastic-memory-lab", "603bb3b"))
        if not cands:
            checked.append({"file": f, "exists": None, "note": "no source key or branch in the cite"})
            problems.append("unresolvable cite for %s (no source key, branch or commit)" % f)
            continue
        hit, tried = None, []
        for wt, s in cands:
            p = os.path.join(work, wt)
            if not os.path.isdir(p):
                tried.append("%s (no worktree)" % wt)
                continue
            fs = tree(p, s)
            if fs is None:
                tried.append("%s@%s (commit not found)" % (wt, s))
                continue
            m = find_in_tree(fs, f)
            tried.append("%s@%s:%s" % (wt, s, "found" if m else "absent"))
            if m:
                hit = (wt, s, m[:3])
                break
        if hit is None:
            # elsewhere at HEAD of any worktree?
            elsewhere = []
            for wt in sorted(set(WORKTREE_OF_BRANCH.values()) | {"elastic-memory-lab"}):
                p = os.path.join(work, wt)
                if os.path.isdir(p):
                    m = find_in_tree(tree(p, "HEAD"), f)
                    if m:
                        elsewhere.append("%s@HEAD:%s" % (wt, m[0]))
            on_disk = [d for d in ("wp-results-aaa0fec", "memcore-out/results", "phasef-out") if
                       os.path.exists(os.path.join(work, d, os.path.basename(f)))]
            problems.append("artifact %s not found at the cited commit (%s)%s" % (
                f, "; ".join(tried), (" but present at " + ", ".join(elsewhere + on_disk)) if elsewhere or on_disk else ""))
        checked.append({"file": f, "found": hit is not None, "tried": tried, "hit": hit})
    return problems, checked


TOK = re.compile(r"\b(?:[A-Z]{1,3}\d+[a-z]?\d*|X\d\d|[a-z0-9_]+\.json|[0-9a-f]{7})\b")
LCLASS = [("overturned", "DID-NOT-HOLD"), ("reviewed", "REVIEWED"), ("owner-reported", "OWNER-REPORTED"),
          ("observed", "OBSERVED"), ("exploratory", "EXPLORATORY"), ("pilot observation", "EXPLORATORY")]


def ledger_rows(text):
    out, section = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        if not line.startswith("|") or line.startswith("|---") or "| Idea |" in line or "| Id |" in line or "| Source |" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        cls = None
        low = line.lower()
        for k, v in LCLASS:
            if k in low:
                cls = v
                break
        out.append({"section": section, "text": line[:300], "class": cls, "tokens": set(TOK.findall(line)) - {"16096"}})
    return out


def kind_conflict(section, lcls, dtag):
    if lcls is None or dtag is None:
        return None
    if dtag == "REVIEWED" and lcls == "OWNER-REPORTED":
        return "LEDGER says owner-reported (stale?) while DESIGN tags REVIEWED"
    if dtag == "OWNER-REPORTED" and lcls == "REVIEWED":
        return "LEDGER says reviewed while DESIGN tags OWNER-REPORTED"
    if section == "Held" and dtag == "DID-NOT-HOLD":
        return "LEDGER Held while DESIGN tags DID-NOT-HOLD"
    if section and section.startswith("Did not hold") and dtag in ("REVIEWED", "OWNER-REPORTED", "EXPLORATORY") :
        return "LEDGER Did-not-hold while DESIGN tags a positive class"
    return None


def main(work, out):
    design_p = os.path.join(work, "elastic-memory-lab", "DESIGN.md")
    text = open(design_p).read()
    planted = "| 9.9.9 | Planted control row. | [OWNER-REPORTED] SYS rep16096_planted_missing.json (feat/systems-skeleton a13b524) |"
    rows = parse_design(text + "\n" + planted + "\n")
    res = []
    for r in rows:
        probs, checked = check_row(r, work)
        res.append(dict(r, problems=probs, checked=checked))
    led = ledger_rows(open(os.path.join(work, "elastic-memory-lab", "LEDGER.md")).read())
    pairs = []
    for r in rows:
        dt = set(TOK.findall(r["evidence"] + " " + r["statement"])) - {"16096"}
        for l in led:
            shared = dt & l["tokens"]
            strong = {t for t in shared if not re.fullmatch(r"[0-9a-f]{7}", t)}
            if len(strong) >= 1 and (len(shared) >= 2 or any(t.endswith(".json") for t in shared) or re.search(r"[A-Z]\d", " ".join(strong))):
                c = kind_conflict(l["section"], l["class"], r["tag"])
                if c:
                    pairs.append({"design_row": r["id"], "design_tag": r["tag"], "ledger_section": l["section"],
                                  "ledger_class": l["class"], "shared_tokens": sorted(shared), "conflict": c,
                                  "ledger_text": l["text"][:200]})
    git_heads = {}
    for wt in sorted(set(WORKTREE_OF_BRANCH.values()) | {"elastic-memory-lab"}):
        p = os.path.join(work, wt)
        if os.path.isdir(p):
            git_heads[wt] = git(p, "rev-parse", "--short", "HEAD")[1].strip()
    real = [x for x in res if x["id"] != "9.9.9"]
    planted_r = next(x for x in res if x["id"] == "9.9.9")
    eng_rows = {"2.2.1", "2.2.2", "2.2.3", "3.11.3", "3.11.10"}
    known10 = [p for p in pairs if p["design_row"] in eng_rows]
    known11 = [p for p in pairs if "semantic_text" in p["ledger_text"]]
    out_obj = {"stamp": {"thresholds_commit": "8c3b5e6", "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
                         "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "design_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
                         "lab_main_head": git_heads.get("elastic-memory-lab"), "worktree_heads": git_heads},
               "witness": {"design_rows_with_tag_cell": len(real), "artifact_refs_checked": sum(len(x["checked"]) for x in real),
                           "ledger_rows": len(led)},
               "tag_counts": dict(__import__("collections").Counter(x["tag"] for x in real)),
               "rows_with_problems": [{k: x[k] for k in ("id", "tag", "evidence", "problems")} for x in real if x["problems"]],
               "ledger_conflicts": pairs,
               "control_planted_row_flagged": bool(planted_r["problems"]), "planted_row_problems": planted_r["problems"],
               "known_positive_section6_item10_rediscovered": bool(known10), "known_positive_section6_item11_rediscovered": bool(known11),
               "all_rows": res}
    json.dump(out_obj, open(out, "w"), indent=1, default=list)
    print(json.dumps({k: v for k, v in out_obj.items() if k not in ("all_rows", "rows_with_problems", "ledger_conflicts")}, indent=1, default=list))
    print("rows with problems:", len(out_obj["rows_with_problems"]), "ledger conflicts:", len(pairs))


if __name__ == "__main__":
    main(*sys.argv[1:3])
