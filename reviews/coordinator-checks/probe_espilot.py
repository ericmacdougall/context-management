"""Main's own attacks on the ES-pilot scorer. argv[1] = dir containing emlab/ (old or new). Temp copies only."""
import copy, gzip, json, os, shutil, sys, tempfile
sys.path.insert(0, sys.argv[1])
from emlab import es_pilot_score as sc
W = "/Users/eric/.agent-harness/work/500523cf-38c7-4f04-96eb-cae1d57638e1/wt-eval/eval/results/"
SRC = W + "exploratory/es_pilot2"
PRIOR = (W + "exploratory/es_pilot.json", W + "pilot/curve_rerank_v2.json")
ROWS = [json.loads(l) for l in gzip.open(SRC + "/rows.jsonl.gz", "rt")]
CE = [json.loads(l) for l in open(SRC + "/ce.jsonl")]
NEW = hasattr(sc, "load_manifest")
def run(name, rows, ce, rehash, pristine=False):
    with tempfile.TemporaryDirectory() as t:
        if pristine:
            shutil.copy(SRC + "/rows.jsonl.gz", t); shutil.copy(SRC + "/ce.jsonl", t)
        else:
            with gzip.open(t + "/rows.jsonl.gz", "wt") as f:
                for r in rows: f.write(json.dumps(r) + "\n")
            with open(t + "/ce.jsonl", "w") as f:
                for c in ce: f.write(json.dumps(c) + "\n")
        if NEW:
            shutil.copy(SRC + "/pilot_manifest.json", t)
            if rehash:
                m = json.load(open(t + "/pilot_manifest.json"))
                m["artifacts"] = {n: sc.file_sha256(os.path.join(t, n)) for n in m["artifacts"]}
                json.dump(m, open(t + "/pilot_manifest.json", "w"))
        try:
            cat = sc.score(t, *PRIOR)["recommendation"]["category"]
        except Exception as e:
            cat = "EXC " + type(e).__name__
        print("%-52s %-8s %s" % (name, "rehash" if rehash else "stamped", cat))
D = copy.deepcopy
run("pristine", None, None, False, pristine=True)
qids = sorted({r["qid"] for r in ROWS}); a, b = qids[3], qids[40]
A = [
 ("drop one question", [r for r in ROWS if r["qid"] != a], [c for c in CE if c["qid"] != a]),
 ("drop one question at H2 only", [r for r in ROWS if not (r["qid"] == a and r["level"] == "H2")], [c for c in CE if not (c["qid"] == a and c["level"] == "H2")]),
 ("duplicate b in place of a (count kept)", [dict(D(r), qid=a) if False else r for r in ROWS if r["qid"] != a] + [dict(D(r)) for r in ROWS if r["qid"] == b], [c for c in CE if c["qid"] != a] + [D(c) for c in CE if c["qid"] == b]),
 ("rename a to new id", [dict(r, qid="zz_new") if r["qid"] == a else r for r in ROWS], [dict(c, qid="zz_new") if c["qid"] == a else c for c in CE]),
 ("all clusters = one", [dict(r, cluster="one") for r in ROWS], CE),
 ("two questions share a's cluster", [dict(r, cluster=next(x["cluster"] for x in ROWS if x["qid"] == a)) if r["qid"] == b else r for r in ROWS], CE),
 ("swap H1/H2 labels", [dict(r, level={"H1": "H2", "H2": "H1"}.get(r["level"], r["level"])) for r in ROWS], [dict(c, level={"H1": "H2", "H2": "H1"}.get(c["level"], c["level"])) for c in CE]),
 ("empty CE", ROWS, []),
 ("CE record for a at H0 removed", ROWS, [c for c in CE if not (c["qid"] == a and c["level"] == "H0")]),
 ("row missing 'exact'", [{k: v for k, v in r.items() if k != "exact"} if i == 5 else r for i, r in enumerate(ROWS)], CE),
 ("row cluster = None", [dict(r, cluster=None) if i == 5 else r for i, r in enumerate(ROWS)], CE),
 ("CE score = inf", ROWS, [dict(D(c), scores={**c["scores"], c["ce_exact"][0]: float("inf")}) if i == 0 else c for i, c in enumerate(CE)]),
 ("empty rows", [], CE),
]
for n, r, c in A:
    for rh in ((False, True) if NEW else (False,)):
        run(n, r, c, rh)
if NEW:
    with tempfile.TemporaryDirectory() as t:
        shutil.copy(SRC + "/rows.jsonl.gz", t); shutil.copy(SRC + "/ce.jsonl", t)
        try: print("no manifest file:", sc.score(t, *PRIOR)["recommendation"]["category"])
        except Exception as e: print("no manifest file: EXC", type(e).__name__)
