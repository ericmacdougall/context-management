"""Independent recomputation of every verdict from the per-item artifacts (proof rule 5). Reads results/*.json and their *.items.*.jsonl.gz,
recomputes thresholds with its own code (no import of the experiment modules, except B1 which replays counterexample traces through the reducer),
and compares with the verdict the experiment printed.
usage: python -m exp.verify [results_dir]"""
import glob
import gzip
import hashlib
import json
import os
import sys


def load(res_dir, name):
    fs = [f for f in glob.glob(os.path.join(res_dir, name + ".*.json")) if ".items." not in f and not os.path.basename(f).startswith("DEV.")]
    out = []
    for f in sorted(fs):
        d = json.load(open(f))
        it = d["result"].get("items")
        if not it:
            continue
        p = os.path.join(res_dir, it["file"])
        raw = open(p, "rb").read()
        assert hashlib.sha256(raw).hexdigest() == it["sha256"], "items hash mismatch " + p
        rows = [json.loads(l) for l in gzip.decompress(raw).decode().splitlines() if l]
        out.append((f, d, rows))
    return out


def by(rows, key):
    o = {}
    for r in rows:
        o.setdefault(r[key], []).append(r)
    return o


def v_e1(rows):
    a = by(rows, "arm")
    en = a["enforced"]
    ok = len(en) >= 20000 and not any(r["structural"] or r["semantic"] for r in en)
    for arm, rs in a.items():
        if not arm.startswith("ctl_"):
            continue
        dim = arm[4:]
        worst_s, worst_m = 9, 9
        for sd, rr in by(rs, "seed").items():
            multi = [r for r in rr if r["n_leaves"] >= 2]
            worst_s = min(worst_s, sum(r["structural"] for r in multi) / len(multi))
            worst_m = min(worst_m, sum(r["semantic"] for r in multi) / len(multi))
        ok &= worst_s >= 0.10 and worst_m >= 0.05 and all(r["bad_dims"] in ([], [dim]) for r in rs)
    return ok


def v_e2(rows):
    a = by(rows, "arm")
    en = a["enforced"]
    ok = len(en) >= 2000 and not any(r["violations"] for r in en)
    cls = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant"]
    ok &= all(sum(r["forbidden_attempts"][c] for r in en) >= 1000 for c in cls)
    ok &= all(sum(r["permitted"][c] for r in en) >= 1000 for c in cls if c != "other_tenant")
    for arm, rs in a.items():
        if arm.startswith("ctl_"):
            ok &= sum(1 for r in rs if r["violations"]) / len(rs) >= 0.10
    return ok


def v_e3(rows):
    c = by(rows, "cat")
    ok = not any(r["ours"] for r in c["planted"]) and not any(r["ours"] for r in c["fault"]) and not any(r["ours"] for r in c["forged"])
    ok &= sum(r["ours"] for r in c["legit"]) >= 0.99 * len(c["legit"]) and len(c["planted"]) >= 2000 and len(c["fault"]) >= 2000 and len(c["legit"]) >= 1000
    ok &= sum(r["naive"] for r in c["planted"]) >= 0.1 * len(c["planted"]) and sum(r["naive"] for r in c["fault"]) >= 0.1 * len(c["fault"])
    ok &= sum(r["naive"] for r in c["forged"]) >= 0.1 * len(c["forged"])
    return ok


def v_e4(rows):
    a = by(rows, "arm")
    f, p, c = a["fenced"], a["positive"], a["control_enqueue_only"]
    return (sum(r["delivered"] for r in f) == 0 and sum(r["escaped"] for r in f) == 0 and all(r["enqueue_allow"] and r["refusal_logged"] for r in f)
            and all(r["delivered"] == 1 for r in p) and sum(r["delivered"] for r in c) >= 0.9 * len(c))


def v_e5(rows):
    a = by(rows, "arm")
    ok = True
    for arm in ("enforced", "notice_none"):
        r = a[arm]
        ok &= not any(x["leak"] or x["notice_wrong"] or x["interference"] for x in r) and sum(x["notice_found"] for x in r) == sum(x["notice_expected"] for x in r)
    ok &= len(a["enforced"]) >= 1000 and sum(x["cross_conflict"] for x in a["enforced"]) >= 0.9 * len(a["enforced"]) and sum(x["notice_found"] for x in a["enforced"]) >= 1000
    ok &= all(x["notice_expected"] == 0 for x in a["notice_none"])
    for arm, rs in a.items():
        if arm.startswith("ctl_"):
            ok &= sum(1 for x in rs if x["leak"]) / len(rs) >= 0.10
    return ok


def v_b2(rows):
    a = by(rows, "arm")
    en = a["enforced"]
    ok = len(en) >= 3000 and all(r["state_equal"] and r["hash_equal"] and r["outcomes_equal"] for r in en) and not any(r["inv_violations"] for r in en)
    ok &= sum(r["contested"] for r in en) >= 0.1 * len(en) and sum(r["snaps"] for r in en) >= 3000
    for arm, rs in a.items():
        if arm.startswith("ctl_"):
            ok &= sum(1 for r in rs if not (r["state_equal"] and r["hash_equal"] and r["outcomes_equal"])) / len(rs) >= 0.10
    return ok


def v_b3(rows):
    sc = by(rows, "scenario")
    ok = True
    for name, rs in sc.items():
        if name.startswith("S"):
            ok &= len(rs) >= 50 and all(r["ok"] for r in rs)
            if name.split("_")[0] in ("S1", "S3", "S5", "S4b"):
                ok &= all(r["overlap"] for r in rs)
    for name, rs in sc.items():
        if not name.startswith("S"):
            ok &= sum(r["ok"] for r in rs) >= 45
    return ok


def v_b1(rows, d):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from exp import b1_modelcheck as b1
    from pol.core import apply, genesis
    from pol.invariants import state_invariants, transition_invariants
    ok = True
    for r in rows:
        if "control" not in r:
            ok &= r["violations"] == 0
            continue
        pool = {e["id"]: e for e in b1.POOL}
        # a trace may name an id that exists twice in the pool (the duplicate proposal id): the n-th occurrence in the trace is the n-th pool event with that id
        st = genesis("T1", b1.PRINCIPALS, b1.AUTHORITY, b1.ADMINS)
        flags = frozenset(r["flags"])
        bad = set()
        seen = {}
        for i in r["trace"]:
            cands = [e for e in b1.POOL if e["id"] == i]
            k = seen.get(i, 0)
            seen[i] = k + 1
            e = b1.deliver(st, cands[min(k, len(cands) - 1)])
            post, code = apply(st, e, flags)
            bad.update(transition_invariants(st, e, post, code) + state_invariants(post))
            st = post
        found = r["expected"] in bad
        ok &= found
    return ok


CHECKS = {"e1_lattice": v_e1, "e2_egress": v_e2, "e3_clash": v_e3, "e4_tighten": v_e4, "e5_threads": v_e5, "b2_rebuild": v_b2, "b3_pg": v_b3}


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    bad = 0
    for name, fn in CHECKS.items():
        for f, doc, rows in load(d, name):
            mine = "PASS" if fn(rows) else "FAIL"
            theirs = doc["result"]["verdict"]
            print("%-12s items=%-8d recomputed=%s printed=%s %s" % (name, len(rows), mine, theirs, "OK" if mine == theirs else "MISMATCH"), os.path.basename(f))
            bad += mine != theirs
    for f, doc, rows in load(d, "b1_modelcheck"):
        mine = "PASS" if v_b1(rows, doc) else "FAIL"
        print("%-12s items=%-8d recomputed=%s printed=%s %s" % ("b1_modelcheck", len(rows), mine, doc["result"]["verdict"], "OK" if mine == doc["result"]["verdict"] else "MISMATCH"), os.path.basename(f))
        bad += mine != doc["result"]["verdict"]
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
