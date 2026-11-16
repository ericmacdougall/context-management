"""Independent recomputation of every experiment verdict from its raw per-item artifact.

usage: python -m verify [results_dir] [--dev]
For each experiment it takes the stamped result files (or, with --dev, only DEV. files), checks the items file hash recorded in the
result, loads verify/<x>.py BY PATH (each module is stdlib-only; an AST check here refuses any module that imports pol, exp or
tests), recomputes the verdict from the rows, and compares it with the verdict the experiment wrote. A disagreement, a missing
artifact or a missing result is FAIL. Output: a table on stdout and results/[DEV.]VERIFY.<sha16>.json."""
import ast
import glob
import gzip
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPS = {"e1": "e1_lattice", "e2": "e2_egress", "e3": "e3_clash", "e4": "e4_tighten", "e5": "e5_threads", "e6": "e6_policy_format",
        "b1": "b1_modelcheck", "b2": "b2_rebuild", "b3": "b3_pg"}
FORBIDDEN = ("pol", "exp", "tests")


def _imports_ok(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for n in ast.walk(tree):
        names = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        if isinstance(n, ast.ImportFrom) and n.level:
            return False
        if any(x.split(".")[0] in FORBIDDEN for x in names):
            return False
    return True


def _load(path):
    spec = importlib.util.spec_from_file_location("verify_" + os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv):
    dev = "--dev" in argv
    args = [a for a in argv if not a.startswith("--")]
    res = args[0] if args else os.path.join(os.path.dirname(HERE), "results")
    table, ok_all = [], True
    for key, name in EXPS.items():
        vpath = os.path.join(HERE, key + ".py")
        files = [f for f in glob.glob(os.path.join(res, ("DEV." if dev else "") + name + ".*.json"))
                 if ".items." not in f and os.path.basename(f).startswith("DEV.") == dev]
        if not files:
            table.append({"exp": name, "verdict": "FAIL", "why": "no result file"})
            ok_all = False
            continue
        if not _imports_ok(vpath):
            table.append({"exp": name, "verdict": "FAIL", "why": "verifier imports pol/exp/tests"})
            ok_all = False
            continue
        mod = _load(vpath)
        for f in sorted(files, key=os.path.getmtime):
            d = json.load(open(f))
            it = d["result"].get("items") or {}
            row = {"exp": name, "file": os.path.basename(f), "commit": d["stamp"].get("commit"), "experiment_verdict": d["result"].get("verdict")}
            try:
                raw = open(os.path.join(res, it["file"]), "rb").read()
                if hashlib.sha256(raw).hexdigest() != it["sha256"]:
                    raise ValueError("items hash mismatch")
                rows = [json.loads(l) for l in gzip.decompress(raw).decode().splitlines() if l]
                out = mod.verify(d, rows)
                row.update(recomputed=out.get("verdict"), rows=len(rows), items_bytes=len(raw))
                row["verdict"] = "PASS" if out.get("verdict") == "PASS" and d["result"].get("verdict") == "PASS" else "FAIL"
                row["agree"] = out.get("verdict") == d["result"].get("verdict")
                row["checks"] = out.get("checks")
            except Exception as e:      # noqa: BLE001  a verifier error is a FAIL, never a skip
                row.update(verdict="FAIL", why="%s: %s" % (type(e).__name__, e))
            ok_all = ok_all and row["verdict"] == "PASS" and row.get("agree", False)
            table.append(row)
    for r in table:
        print("%-18s %-5s exp=%-5s recomputed=%-5s agree=%s %s" % (r["exp"], r["verdict"], r.get("experiment_verdict"), r.get("recomputed"),
                                                                    r.get("agree"), r.get("file", r.get("why", ""))))
    doc = json.dumps({"overall": "PASS" if ok_all else "FAIL", "dev": dev, "rows": table}, indent=1, sort_keys=True, default=str).encode()
    h = hashlib.sha256(doc).hexdigest()
    out = os.path.join(res, "%sVERIFY.%s.json" % ("DEV." if dev else "", h[:16]))
    open(out, "wb").write(doc)
    print("VERIFY overall=%s sha256=%s file=%s" % ("PASS" if ok_all else "FAIL", h, out))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
