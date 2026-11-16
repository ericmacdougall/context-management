"""DESIGN 3.9.1: "the core depends on no vendor" (THRESHOLDS.md section O). Static import graph over memcore at a given
tree (exported with git archive). Core = memcore/*.py at top level; adapters/, hosts/, stubs/, remote/, tests/ are not
core. A violation is a core module that imports, directly or transitively through other core modules, a vendor SDK or a
non-core memcore package. Lazy imports (inside functions) count and are labelled. Vendor-name string literals in core
are reported separately as information, not as violations.
A mutant tree (one vendor import added to memcore/ports.py) must be flagged (control).

usage: python import_graph.py <tree root containing memcore/> <commit label> <out.json>
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time

VENDOR = ("anthropic", "openai", "elasticsearch", "elastic_transport", "boto3", "botocore", "google", "temporalio",
          "cohere", "mistralai", "vertexai", "azure", "langchain", "llama_index")
NONCORE = ("memcore.adapters", "memcore.hosts", "memcore.stubs", "memcore.remote", "memcore.tests")
STR_HINT = re.compile(r"anthropic\.com|openai\.com|api\.openai|claude|codex|_inference|elastic\.co|bedrock", re.I)


def modules(root):
    out = {}
    base = os.path.join(root, "memcore")
    for dp, dn, fn in os.walk(base):
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, root)[:-3].replace(os.sep, ".")
                if rel.endswith(".__init__"):
                    rel = rel[: -len(".__init__")]
                out[rel] = p
    return out


def is_core(m):
    return m.startswith("memcore") and m.count(".") <= 1 and not m.startswith(NONCORE)


def imports_of(mod, path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    res, strings = [], []
    pkg = mod if path.endswith("__init__.py") else mod.rsplit(".", 1)[0]

    def walk(node, lazy):
        for ch in ast.iter_child_nodes(node):
            lz = lazy or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(ch, ast.Import):
                for a in ch.names:
                    res.append((a.name, lz, ch.lineno))
            elif isinstance(ch, ast.ImportFrom):
                if ch.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (ch.level - 1)] if ch.level > 1 else base
                    target = ".".join(base + ([ch.module] if ch.module else []))
                else:
                    target = ch.module or ""
                res.append((target, lz, ch.lineno))
                for a in ch.names:  # from memcore import adapters
                    res.append((target + "." + a.name, lz, ch.lineno))
            elif isinstance(ch, ast.Constant) and isinstance(ch.value, str) and STR_HINT.search(ch.value):
                strings.append((ch.value[:80], ch.lineno))
            walk(ch, lz)
    walk(tree, False)
    return res, strings


def analyse(root):
    mods = modules(root)
    graph, strs = {}, {}
    for m, p in mods.items():
        graph[m], strs[m] = imports_of(m, p)
    direct = {}
    for m in mods:
        if not is_core(m):
            continue
        v = []
        for tgt, lazy, ln in graph[m]:
            top = tgt.split(".")[0]
            if top in VENDOR:
                v.append({"import": tgt, "kind": "vendor", "lazy": lazy, "line": ln})
            elif tgt.startswith(NONCORE) and (tgt in mods or any(tgt.startswith(x + ".") or tgt == x for x in mods)):
                v.append({"import": tgt, "kind": "non-core memcore", "lazy": lazy, "line": ln})
        direct[m] = v
    # transitive through core modules
    trans = {}
    for m in direct:
        seen, stack, found = {m}, [m], []
        while stack:
            x = stack.pop()
            for f in direct.get(x, []):
                found.append(dict(f, via=x))
            for tgt, _, _ in graph.get(x, []):
                if tgt in direct and tgt not in seen:
                    seen.add(tgt)
                    stack.append(tgt)
        trans[m] = found
    return {"core_modules": sorted(direct), "noncore_modules": sorted(m for m in mods if not is_core(m)),
            "direct_violations": {m: v for m, v in direct.items() if v},
            "transitive_violations": {m: v for m, v in trans.items() if v},
            "vendor_string_literals_in_core": {m: s for m, s in strs.items() if is_core(m) and s}}


def main(root, label, out):
    real = analyse(root)
    tmp = tempfile.mkdtemp()
    try:
        shutil.copytree(os.path.join(root, "memcore"), os.path.join(tmp, "memcore"))
        p = os.path.join(tmp, "memcore", "ports.py")
        src = open(p).read()
        open(p, "w").write("import anthropic  # mutant\n" + src)
        mut = analyse(tmp)
    finally:
        shutil.rmtree(tmp)
    res = {"stamp": {"thresholds_commit": "8c3b5e6", "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tree": label},
           "witness": {"core_modules": len(real["core_modules"]), "noncore_modules": len(real["noncore_modules"])},
           "result": real,
           "control_mutant_flagged": "memcore.ports" in mut["direct_violations"],
           "threshold_checks": {"zero_violations": not real["transitive_violations"],
                                "mutant_flagged": "memcore.ports" in mut["direct_violations"]}}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({"witness": res["witness"], "direct": real["direct_violations"],
                      "checks": res["threshold_checks"]}, indent=1)[:4000])


if __name__ == "__main__":
    main(*sys.argv[1:4])
