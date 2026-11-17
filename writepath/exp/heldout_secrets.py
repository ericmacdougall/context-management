"""Held-out secret-scan test (THRESHOLDS amendment 15). One stamped run inside run_all; no stores are touched.

H1: exp/heldout_secrets_gen.py, an independent generator (positives and negatives). H2: the ingest stream's held-out
transforms (exp/heldout_ingest_variants.py, vendored verbatim). Judge: wp.secrets.scan(text) non-empty = caught.
Control: scan(text, decode=False) on H1 in-scope value-transform items. Rows carry a 16-hex sha256 of each text and
never the text or the planted value; the document is checked for every planted value before it is written.

usage: python -m exp.heldout_secrets            the stamped run (refuses unless the scanner blob is the pinned one)
       python -m exp.heldout_secrets --selftest plumbing check with a STUB scanner (scores nothing real, writes no
                                                result); used by the shake-down so no held-out item is scored early"""
import hashlib
import json
import math
import pathlib
import sys

from exp import heldout_ingest_variants as H2
from exp import heldout_secrets_gen as G

HERE = pathlib.Path(__file__).resolve().parent
SCANNER_BLOB = "58b025fcb5b6266077c586436e00acf247d97854"
GEN_SHA256 = "bc731ed07cab94d700303a473c9a758180c85c8c1abfb775e6e00af054bf65c1"
OUT_OF_SCOPE = {"reversed"}                                   # row 19's docstring at 4960f50 (amendment 15)
# the generator's value transforms (its docstring), restricted to amendment 15's control list: base64*, hex*, url_*,
# json_* value transforms, quoted_printable, html_entity_*, double_base64, base64_of_assignment. json_document is a
# syntax wrapper in the generator, not a value transform, so it is not in the control set.
CONTROL_ENCODINGS = {"base64", "base64_nopad", "base64url", "base64url_nopad", "base64_wrap64", "base64_wrap76",
                     "hex_lower", "hex_upper", "url_encoded", "url_double_encoded", "json_escaped",
                     "json_unicode_escaped", "json_nested_escaped", "quoted_printable", "html_entity_named",
                     "html_entity_numeric", "double_base64", "base64_of_assignment"}


def wilson(k, n, z=1.96):
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - m) / d, 4), round((c + m) / d, 4)]


def _blob(p):
    data = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _score(items, scan, setname):
    rows = []
    for i, it in enumerate(items):
        f = scan(it["text"])
        rows.append({"set": setname, "family": it["family"], "encoding": it.get("encoding", ""), "context": it.get("context", ""),
                     "index": it.get("index", i), "caught": bool(f), "detectors": sorted(f), "text_sha16": _h(it["text"]),
                     "in_scope": it.get("encoding", "") not in OUT_OF_SCOPE})
    return rows


def _rate(rows):
    k, n = sum(r["caught"] for r in rows), len(rows)
    return {"n": n, "caught": k, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def _cells(rows, key):
    out = {}
    for r in rows:
        c = out.setdefault(r[key], [0, 0])
        c[0] += r["caught"]
        c[1] += 1
    return {k: {"caught": a, "n": b} for k, (a, b) in sorted(out.items())}


def run(scan, scan_nodecode):
    pos1, neg1, pos2 = G.positives(16096), G.negatives(16096), H2.positives()
    r1 = _score(pos1, scan, "H1")
    rn = _score(neg1, scan, "H1neg")
    r2 = _score(pos2, scan, "H2")
    ctl_items = [it for it in pos1 if it["encoding"] in CONTROL_ENCODINGS]
    rc_ship = [r for r in r1 if r["encoding"] in CONTROL_ENCODINGS]
    rc_ctl = _score(ctl_items, scan_nodecode, "H1ctl_nodecode")
    h1_in, h2_in = [r for r in r1 if r["in_scope"]], [r for r in r2 if r["in_scope"]]
    out = {
        "H1_in_scope": _rate(h1_in), "H1_all": _rate(r1), "H1_out_of_scope": _rate([r for r in r1 if not r["in_scope"]]),
        "H2_in_scope": _rate(h2_in), "H2_all": _rate(r2), "H2_out_of_scope": _rate([r for r in r2 if not r["in_scope"]]),
        "H1_negatives_blocked": _rate(rn),
        "control": {"items": len(ctl_items), "shipped": _rate(rc_ship), "nodecode": _rate(rc_ctl)},
        "H1_by_family": _cells(r1, "family"), "H1_by_encoding": _cells(r1, "encoding"), "H2_by_encoding": _cells(r2, "encoding"),
        "H1neg_blocked_by_family": {k: v for k, v in _cells(rn, "family").items() if v["caught"]},
        "row19_planted_for_comparison": "880/880 on the author's own planted corpus (4960f50)",
    }
    ship_rate, ctl_rate = out["control"]["shipped"]["rate"] or 0, out["control"]["nodecode"]["rate"] or 0
    out["control"]["gap"] = round(ship_rate - ctl_rate, 4)
    why = []
    if out["H1_in_scope"]["wilson95"][0] < 0.95:
        why.append(f"H1 in-scope Wilson lower {out['H1_in_scope']['wilson95'][0]} < 0.95")
    if out["H2_in_scope"]["wilson95"][0] < 0.95:
        why.append(f"H2 in-scope Wilson lower {out['H2_in_scope']['wilson95'][0]} < 0.95")
    if out["H1_negatives_blocked"]["wilson95"][1] > 0.02:
        why.append(f"H1 false-block Wilson upper {out['H1_negatives_blocked']['wilson95'][1]} > 0.02")
    if why:
        out["verdict"] = "FAIL"
    elif out["control"]["gap"] < 0.10:
        out["verdict"] = "INCONCLUSIVE"
        why.append(f"control gap {out['control']['gap']} < 0.10 (harness not shown sensitive)")
    else:
        out["verdict"] = "PASS"
    out["why"] = why
    out["rows"] = r1 + rn + r2 + rc_ctl
    forbidden = [s for it in pos1 + pos2 for s in (it["secret"], it.get("value", ""), it["text"]) if s and len(s) >= 8]
    forbidden += [it["text"] for it in neg1 if len(it["text"]) >= 8]
    return out, forbidden


def _assert_clean(doc, forbidden):
    raw = json.dumps(doc, default=str)
    leaks = sum(1 for s in forbidden if s in raw)
    if leaks:
        raise SystemExit(f"refusing to write: {leaks} planted values or texts occur in the result document")
    return len(forbidden)


def main(selftest):
    gen_sha = hashlib.sha256((HERE / "heldout_secrets_gen.py").read_bytes()).hexdigest()
    if gen_sha != GEN_SHA256 or G.VERSION != "heldout-secrets-gen-1.0.0-2026-11-17":
        raise SystemExit("refusing: the H1 generator is not the pinned file")
    if selftest:                      # plumbing only: a stub scanner, nothing real is scored, nothing is written
        stub = lambda t, decode=True: {"stub"} if (hash(t) % 2) else set()  # noqa: E731
        out, forbidden = run(stub, lambda t: stub(t, False))
        n = _assert_clean(out, forbidden)
        print("selftest ok", {k: out[k]["n"] for k in ("H1_all", "H2_all", "H1_negatives_blocked")},
              "control items", out["control"]["items"], "rows", len(out["rows"]), "forbidden checked", n)
        return
    from wp import secrets as SEC
    blob = _blob(HERE.parent / "wp" / "secrets.py")
    if blob != SCANNER_BLOB:
        raise SystemExit(f"refusing: wp/secrets.py is blob {blob}, amendment 15 pins {SCANNER_BLOB}")
    out, forbidden = run(SEC.scan, lambda t: SEC.scan(t, decode=False))
    out["scanner_blob"], out["scanner_version"], out["generator_sha256"] = blob, SEC.VERSION, gen_sha
    out["forbidden_values_checked"] = _assert_clean(out, forbidden)
    from exp.common import write_result
    print("heldout_secrets", out["verdict"], json.dumps({k: out[k] for k in ("H1_in_scope", "H2_in_scope", "H1_negatives_blocked",
                                                                          "H1_out_of_scope", "H2_out_of_scope", "control", "why")}))
    write_result("heldout_secrets", out)


if __name__ == "__main__":
    main("--selftest" in sys.argv)
