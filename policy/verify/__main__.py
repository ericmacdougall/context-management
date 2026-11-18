"""Independent recomputation of every experiment verdict from its raw per-item artifact.

usage: python -m verify [results_dir] [--dev] [--seeds-file F] [--thresholds-file F]
For each experiment it takes the stamped result files (or, with --dev, only DEV. files), checks the items file hash recorded in the
result, loads verify/<x>.py BY PATH (each module is stdlib-only; an AST check here refuses any module that imports pol, exp or
tests, and checks the shared evidence layer verify/evidence.py, which every verifier loads by path, the same way), recomputes the
verdict from the rows, and compares it with the verdict the experiment wrote. A disagreement, a missing
artifact or a missing result is FAIL.
For a STAMPED set (no --dev) the provenance gate verify/provenance.py must also pass (review-3 N6: one result per experiment, clean
stamps at the thresholds commit, one commit, one thresholds digest equal to the THRESHOLDS file, the STAMPED seeds of exp/seeds.py);
--seeds-file and --thresholds-file name the files as they were at the run commit when verifying an older set. VERIFY records the
provenance report next to the per-experiment rows, so a row-level PASS stays distinguishable from provenance acceptance.
Output: a table on stdout and results/[DEV.]VERIFY.<sha16>.json."""
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
        "b1": "b1_modelcheck", "b2": "b2_rebuild", "b3": "b3_pg", "pgdiff": "pg_diff", "mig": "mig_rehearsal",
        "s2": "s2_sessions"}   # amendment 5 adds pgdiff and mig; amendment 6 adds s2
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


def _opts(argv):
    dev, files, args, i = False, {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--dev":
            dev = True
        elif a in ("--seeds-file", "--thresholds-file"):
            if i + 1 >= len(argv):
                raise SystemExit("%s needs a file" % a)
            files[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            raise SystemExit("unknown option %s" % a)
        else:
            args.append(a)
        i += 1
    return dev, files, args


def main(argv):
    dev, opt_files, args = _opts(argv)
    root = os.path.dirname(HERE)
    res = args[0] if args else os.path.join(root, "results")
    table, ok_all, stamped_docs = [], True, {}
    try:                                                              # GoodQA #16096 class B: the layer every verifier loads
        evidence_ok = _imports_ok(os.path.join(HERE, "evidence.py"))
    except (OSError, SyntaxError):                                    # missing or unparsable: every experiment FAILs, no crash
        evidence_ok = False
    for key, name in EXPS.items():
        vpath = os.path.join(HERE, key + ".py")
        files = [f for f in glob.glob(os.path.join(res, ("DEV." if dev else "") + name + ".*.json"))
                 if ".items." not in f and os.path.basename(f).startswith("DEV.") == dev]
        if not files:
            table.append({"exp": name, "verdict": "FAIL", "why": "no result file"})
            ok_all = False
            continue
        if not _imports_ok(vpath) or not evidence_ok:
            table.append({"exp": name, "verdict": "FAIL", "why": "verifier or verify/evidence.py imports pol/exp/tests"})
            ok_all = False
            continue
        mod = _load(vpath)
        for f in sorted(files, key=os.path.getmtime):
            d = json.load(open(f))
            stamped_docs.setdefault(name, []).append(d)
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
    rows_ok = ok_all
    if dev:
        prov = {"ok": None, "problems": [], "note": "not checked: DEV set"}
    else:
        ppath = os.path.join(HERE, "provenance.py")
        if not _imports_ok(ppath):
            prov = {"ok": False, "problems": ["verify/provenance.py imports pol/exp/tests"]}
        else:
            prov = _load(ppath).check(stamped_docs, EXPS, opt_files.get("seeds-file") or os.path.join(root, "exp", "seeds.py"),
                                      opt_files.get("thresholds-file") or os.path.join(root, "THRESHOLDS.md"))
        ok_all = ok_all and prov["ok"] is True
        print("PROVENANCE %s %s" % ("PASS" if prov["ok"] else "FAIL", "; ".join(prov["problems"])))
    doc = json.dumps({"overall": "PASS" if ok_all else "FAIL", "dev": dev, "rows_pass": rows_ok, "provenance": prov, "rows": table},
                     indent=1, sort_keys=True, default=str).encode()
    h = hashlib.sha256(doc).hexdigest()
    out = os.path.join(res, "%sVERIFY.%s.json" % ("DEV." if dev else "", h[:16]))
    os.makedirs(res, exist_ok=True)              # a run stopped before any experiment (disk guard) still gets its FAIL record
    with open(out, "wb") as fh:
        fh.write(doc)
    print("VERIFY overall=%s sha256=%s file=%s" % ("PASS" if ok_all else "FAIL", h, out))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
