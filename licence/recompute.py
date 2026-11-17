"""Recompute experiment-2 and experiment-1 moment figures from the RAW per-request rows (own code: no import of oracle.py, exp2.py or memcore).
Usage: recompute.py RESULTS_DIR OUT.json     (reads <mode>_<n>.json, .rows.gz, and when present .oracle.gz and .poll.gz)
Lives without an .oracle.gz (the first five counted lives) cannot have T1 recomputed: that is reported, not hidden."""
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe  # noqa: E402

NOT_OBSERVED = ("SKIP", "UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS")   # neither success nor refusal; mirrors matrix.NOT_OBSERVED (tested)


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


def agree(a, b):
    """Two figures agree only when both are present and equal (floats within 1e-6), or both are absent. None never equals 0."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return abs(a - b) < 1e-6
    return a == b


def one(path):
    base = path[:-5]
    R = json.load(open(path))
    mode = R["mode"]
    rows = [json.loads(l) for l in gzip.open(base + ".rows.gz", "rt")]
    t_req, t_resp = (R["start_basic"]["t_req"], R["start_basic"]["t_resp"]) if mode == "down" else (R["start_trial"]["t_req"], R["start_trial"]["t_resp"])
    out = {"file": os.path.basename(path), "mode": mode, "rows": len(rows)}
    ev2 = R.get("exp2", {}).get("events", {})
    window = (ev2.get("t_start"), ev2.get("t_stop"))
    try:   # review 9 F17: every figure below is computed from the TYPED stream (order, window, closed domains), never from raw lists
        W = observe.parse_work(rows, window)
    except observe.Unparsed as e:
        out["NOT RECOMPUTABLE"] = "workload rows do not parse: %s" % e
        return out
    arms = {}
    for w in W:
        a = arms.setdefault(w.arm, [0, 0])
        a[0] += 1
        a[1] += 0 if w.ok else 1
    out["arms_n_fail"] = arms
    av = observe.availability(W)
    out["T2_failed"], out["T2_longest_window_s"] = av["T2_failed_requests"], av["T2_longest_failure_window_s"]
    fl = observe.flip(W, t_req, t_resp, mode)
    out["T3_flip_s"] = fl["T3_flip_latency_s"]
    lic = lambda w: w.outcome is not None and w.outcome.case == "LICENCE_REFUSAL"
    ctl = [w for w in W if w.arm == "ctl"]
    if mode == "down":
        out["T3_backend_labelled_started_after_resp"] = sum(1 for w in W if w.arm == "auto" and w.ok and w.label == "backend" and w.t0 > t_resp)
        fails_c, succ_c = [w for w in ctl if lic(w)], [w for w in ctl if w.ok]
        out["ctl_first_licence_failure_start_rel_resp_s"] = min(w.t0 for w in fails_c) - t_resp if fails_c else None
        out["ctl_last_success_start_rel_resp_s"] = max(w.t0 for w in succ_c) - t_resp if succ_c else None
        out["ctl_success_started_after_resp"] = sum(1 for w in succ_c if w.t0 > t_resp)
        late = [w for w in ctl if w.t0 >= t_resp + 1.0]
        out["T5_fail_fraction"] = sum(1 for w in late if not w.ok) / max(1, len(late))
        out["T5_licence_n"] = len(late)   # addendum C / review 8: counted by Outcome case (LICENCE_REFUSAL), not by substring
        out["T5_licence_fail_fraction"] = sum(1 for w in late if lic(w)) / max(1, len(late))
    else:
        out["T3_client_after_first_backend"] = fl["T3_client_after_first_backend"]
        pre = [w for w in ctl if w.t1 < t_req]
        out["T5_licence_n"] = len(pre)
        out["T5_licence_fail_fraction"] = sum(1 for w in pre if lic(w)) / max(1, len(pre))
        out["ctl_before_trial_fail_fraction"] = sum(1 for w in pre if not w.ok) / max(1, len(pre))
        later = [w.t0 for w in ctl if w.ok and w.t0 > t_req]
        out["ctl_first_success_start_rel_resp_s"] = min(later) - t_resp if later else None
    # T1 from the saved oracle
    if os.path.exists(base + ".oracle.gz"):
        O = json.load(gzip.open(base + ".oracle.gz", "rt"))
        legs, orrf, olin = O["legs"], O["rrf"], O["linear"]
        examined = wrong = 0
        ctl_n = ctl_flag = 0
        for w in W:
            r = rows[w.seq]   # selection on the typed row; query index and returned ids are payload of the same row
            if not w.ok or w.arm not in ("auto", "lin", "brk"):
                continue
            q = str(r[3])
            if w.label == "client":
                ok = legal_topk(r[6], legs[q])
            else:
                ref = (orrf if w.arm in ("auto", "brk") else olin).get(q)
                ok = ref is not None and r[6] == ref
            if w.arm == "brk":
                if w.label == "client":
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
        try:   # the poll stream and every per-probe [start, end] pair are typed: finite, inside the run window, start <= end
            observe.parse_poll(P, window)
            for i, p in enumerate(P):
                for f, se in (p.get("_times") or {}).items():
                    if type(se) is not list or len(se) != 2:
                        raise observe.Unparsed(("poll", i, "_times", f), "%r is not [start, end]" % (se,))
                    a0, a1 = observe._time(se[0], ("poll", i, "_times", f, 0), window), observe._time(se[1], ("poll", i, "_times", f, 1), window)
                    if a0 > a1:
                        raise observe.Unparsed(("poll", i, "_times", f), "start after end")
        except observe.Unparsed as e:
            P, out["moment_per_probe_times_error"] = [], "poll rows do not parse: %s" % e
        for f in ("rrf", "linear", "semantic_query", "inference_use", "dls_search"):
            ev = [(p["_times"][f][0], p["_times"][f][1], "AVAILABLE" if observe.parse_cell(p[f], ()).success else p[f][0], observe.parse_cell(p[f], ()).outcome)
                  for p in P if f in p and "_times" in p and f in p["_times"]]   # a success is any 2xx answer, by Outcome (review 8)
            if not ev:
                continue
            if mode == "down":
                # the probe's required refusal by Outcome case (observe.REQUIRED_REFUSAL), not any non-AVAILABLE class (review 8 F13)
                refused = [e for e in ev if e[3] is not None and e[3].case == observe.REQUIRED_REFUSAL[f] and e[1] >= t_resp - 0.5]
                ok_after = [e for e in ev if e[2] == "AVAILABLE" and e[0] >= t_resp]
                fr = min(refused, key=lambda e: e[:2]) if refused else None
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
    out["agrees_with_harness"] = {k: agree(a, b) for k, (a, b) in cmp.items()}
    return out


def main():
    d, o = sys.argv[1], sys.argv[2]
    res = [one(f) for f in sorted(glob.glob(os.path.join(d, "down_[0-9]*.json")) + glob.glob(os.path.join(d, "up_[0-9]*.json")))]
    json.dump(res, open(o, "w"), indent=1, sort_keys=True)
    for r in res:
        print(r["file"], "agree:", all(r["agrees_with_harness"].values()), "T1:", r.get("T1_examined", r.get("T1")), r.get("T1_mismatches"), "flip", r["T3_flip_s"])


if __name__ == "__main__":
    main()
