"""Recompute experiment-2 and experiment-1 moment figures from the RAW per-request rows (own code: no import of oracle.py, exp2.py or memcore).
Usage: recompute.py RESULTS_DIR OUT.json     (reads <mode>_<n>.json, .rows.gz, and when present .oracle.gz and .poll.gz)
Lives without an .oracle.gz (the first five counted lives) cannot have T1 recomputed: that is reported, not hidden."""
import glob
import gzip
import json
import os
import sys


def rrf(lists, c=60):
    s = {}
    for lst in lists:
        seen = set()
        for r, d in enumerate(lst, 1):
            if d not in seen:
                seen.add(d)
                s[d] = s.get(d, 0.0) + 1.0 / (c + r)
    return s


def legal_topk(ids, lists, k=10):
    sc = rrf(lists)
    n = min(k, len(sc))
    if len(ids) != n or len(set(ids)) != n or any(i not in sc for i in ids):
        return False
    kth = sorted(sc.values(), reverse=True)[n - 1]
    got = [sc[i] for i in ids]
    if any(got[i] < got[i + 1] - 1e-12 for i in range(len(got) - 1)) or any(g < kth - 1e-12 for g in got):
        return False
    return {d for d, v in sc.items() if v > kth + 1e-12} <= set(ids)


def one(path):
    base = path[:-5]
    R = json.load(open(path))
    mode = R["mode"]
    rows = [json.loads(l) for l in gzip.open(base + ".rows.gz", "rt")]
    t_req, t_resp = (R["start_basic"]["t_req"], R["start_basic"]["t_resp"]) if mode == "down" else (R["start_trial"]["t_req"], R["start_trial"]["t_resp"])
    out = {"file": os.path.basename(path), "mode": mode, "rows": len(rows)}
    arms = {}
    for r in rows:
        a = arms.setdefault(r[2], [0, 0])
        a[0] += 1
        a[1] += 0 if r[4] else 1
    out["arms_n_fail"] = arms
    fails = sorted(r for r in rows if not r[4] and r[2] in ("auto", "lin", "lex", "vec"))
    cur, longest = None, 0.0
    for r in fails:
        if cur and r[0] - cur[1] < 1.0:
            cur[1] = max(cur[1], r[1])
        else:
            cur = [r[0], r[1]]
        longest = max(longest, cur[1] - cur[0])
    out["T2_failed"], out["T2_longest_window_s"] = len(fails), longest
    auto = sorted(r for r in rows if r[2] == "auto" and r[4])
    if mode == "down":
        first = next((r for r in auto if r[5] == "client" and r[1] >= t_req), None)
        out["T3_flip_s"] = None if first is None else first[1] - t_resp
        out["T3_backend_labelled_started_after_resp"] = sum(1 for r in auto if r[5] == "backend" and r[0] > t_resp)
        ctl = sorted(r for r in rows if r[2] == "ctl")
        fails_c = [r for r in ctl if not r[4] and "non-compliant" in (r[7] or "")]
        succ_c = [r for r in ctl if r[4]]
        out["ctl_first_licence_failure_start_rel_resp_s"] = min(r[0] for r in fails_c) - t_resp if fails_c else None
        out["ctl_last_success_start_rel_resp_s"] = max(r[0] for r in succ_c) - t_resp if succ_c else None
        out["ctl_success_started_after_resp"] = sum(1 for r in succ_c if r[0] > t_resp)
        late = [r for r in ctl if r[0] >= t_resp + 1.0]
        out["T5_fail_fraction"] = sum(1 for r in late if not r[4]) / max(1, len(late))
    else:
        first = next((r for r in auto if r[5] == "backend" and r[1] >= t_resp), None)
        out["T3_flip_s"] = None if first is None else first[1] - t_resp
        out["T3_client_after_first_backend"] = sum(1 for r in auto if first and r[5] == "client" and r[0] > first[1])
        ctl = [r for r in rows if r[2] == "ctl"]
        out["ctl_before_trial_fail_fraction"] = sum(1 for r in ctl if r[1] < t_req and not r[4]) / max(1, sum(1 for r in ctl if r[1] < t_req))
        out["ctl_first_success_start_rel_resp_s"] = min([r[0] for r in ctl if r[4] and r[0] > t_req] or [None]) - t_resp if any(r[4] and r[0] > t_req for r in ctl) else None
    # T1 from the saved oracle
    if os.path.exists(base + ".oracle.gz"):
        O = json.load(gzip.open(base + ".oracle.gz", "rt"))
        legs, orrf, olin = O["legs"], O["rrf"], O["linear"]
        examined = wrong = 0
        ctl_n = ctl_flag = 0
        for r in rows:
            if not r[4] or r[2] not in ("auto", "lin", "brk"):
                continue
            q = str(r[3])
            if r[5] == "client":
                ok = legal_topk(r[6], legs[q])
            else:
                ref = (orrf if r[2] in ("auto", "brk") else olin).get(q)
                ok = ref is not None and r[6] == ref
            if r[2] == "brk":
                if r[5] == "client":
                    ctl_n += 1
                    ctl_flag += 0 if ok else 1
                continue
            examined += 1
            wrong += 0 if ok else 1
        out["T1_examined"], out["T1_mismatches"], out["T1_control_client_n"], out["T1_control_flagged"] = examined, wrong, ctl_n, ctl_flag
    else:
        out["T1"] = "NOT RECOMPUTABLE: oracle legs were not saved for this life"
    if os.path.exists(base + ".poll.gz"):
        P = json.load(gzip.open(base + ".poll.gz", "rt"))
        feats = {}
        for f in ("rrf", "linear", "semantic_query", "inference_use", "dls_search"):
            ev = [(p["_times"][f][0], p["_times"][f][1], p[f][0]) for p in P if f in p and "_times" in p and f in p["_times"]]
            if not ev:
                continue
            if mode == "down":
                refused = [e for e in ev if e[2] != "AVAILABLE" and e[2] != "SKIP" and e[1] >= t_resp - 0.5]
                ok_after = [e for e in ev if e[2] == "AVAILABLE" and e[0] >= t_resp]
                fr = min(refused) if refused else None
                feats[f] = {"first_refusal_probe_start_rel_resp_s": None if fr is None else fr[0] - t_resp, "first_refusal_probe_end_rel_resp_s": None if fr is None else fr[1] - t_resp,
                            "last_success_probe_start_rel_resp_s": max([e[0] for e in ev if e[2] == "AVAILABLE"] or [None]) - t_resp if any(e[2] == "AVAILABLE" for e in ev) else None,
                            "successes_started_after_resp": len(ok_after), "polls": len(ev)}
            else:
                okk = [e for e in ev if e[2] == "AVAILABLE" and e[1] >= t_resp]
                fo = min(okk) if okk else None
                feats[f] = {"first_success_probe_start_rel_resp_s": None if fo is None else fo[0] - t_resp, "first_success_probe_end_rel_resp_s": None if fo is None else fo[1] - t_resp, "polls": len(ev)}
        out["moment_per_probe_times"] = feats
    else:
        out["moment_per_probe_times"] = "NOT RECOMPUTABLE: per-probe times were not saved for this life"
    # compare with the harness's own figures
    e = R["exp2"]
    cmp = {"T2_failed": (out["T2_failed"], e["T2_failed_requests"]), "T3_flip_s": (out["T3_flip_s"], e["T3_flip_latency_s"])}
    if "T1_examined" in out:
        cmp["T1_examined"] = (out["T1_examined"], e["T1_examined"])
        cmp["T1_mismatches"] = (out["T1_mismatches"], e["T1_mismatches"])
    out["agrees_with_harness"] = {k: (abs((a or 0) - (b or 0)) < 1e-6 if isinstance(a, float) or isinstance(b, float) else a == b) for k, (a, b) in cmp.items()}
    return out


def main():
    d, o = sys.argv[1], sys.argv[2]
    res = [one(f) for f in sorted(glob.glob(os.path.join(d, "down_[0-9]*.json")) + glob.glob(os.path.join(d, "up_[0-9]*.json")))]
    json.dump(res, open(o, "w"), indent=1, sort_keys=True)
    for r in res:
        print(r["file"], "agree:", all(r["agrees_with_harness"].values()), "T1:", r.get("T1_examined", r.get("T1")), r.get("T1_mismatches"), "flip", r["T3_flip_s"])


if __name__ == "__main__":
    main()
