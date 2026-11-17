"""J1 scorer. Recomputes everything from the committed rows; WITHHOLDS (status WITHHELD, no verdict) when the evidence is incomplete:
a missing or duplicate item, a judge row whose returned model id is not the registered judge, a missing audit row, a control short of 20.
Bars come from stage11_bars.json (committed alone before the first real call); the scorer refuses to run without it.
usage: python -m pilots.j1_score ITEMS.json JUDGE.jsonl CONTROL.jsonl AUDIT.jsonl BARS.json OUT.json
Rows: judge/control {"item_id","verdict":true|false,"model_returned":[...],"key"}; audit {"item_id","verdict":true|false,"key"}.
"""
import json
import sys
from math import sqrt

JUDGE_MODEL = "claude-opus-5-5"
N_WRONG, N_PARA, N_BORDER, N_CONTROL = 60, 60, 40, 20


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    hw = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - hw, c + hw)


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _index(rows, what, problems):
    out = {}
    for r in rows:
        if r["item_id"] in out:
            problems.append("duplicate %s row %s" % (what, r["item_id"]))
        out[r["item_id"]] = r
    return out


def score(items, judge_rows, control_rows, audit_rows, bars):
    problems = []
    cats = {"wrong": N_WRONG, "paraphrase": N_PARA, "borderline": N_BORDER}
    by_cat = {c: [i for i in items if i["category"] == c] for c in cats}
    for c, n in cats.items():
        if len(by_cat[c]) != n:
            problems.append("%s items: %d, expected %d" % (c, len(by_cat[c]), n))
        if len({i["cluster"] for i in by_cat[c]}) != len(by_cat[c]):
            problems.append("%s items do not come from distinct clusters" % c)
    jr, cr, ar = _index(judge_rows, "judge", problems), _index(control_rows, "control", problems), _index(audit_rows, "audit", problems)
    for i in items:
        r = jr.get(i["item_id"])
        if r is None:
            problems.append("missing judge row %s" % i["item_id"])
        elif r.get("model_returned") != [JUDGE_MODEL] and JUDGE_MODEL not in r.get("model_returned", []):
            problems.append("judge row %s returned model %s" % (i["item_id"], r.get("model_returned")))
    for i in by_cat["borderline"]:
        if i["item_id"] not in ar:
            problems.append("missing audit row %s" % i["item_id"])
    if len(cr) != N_CONTROL:
        problems.append("control rows: %d, expected %d" % (len(cr), N_CONTROL))
    for r in cr.values():
        if JUDGE_MODEL not in r.get("model_returned", []):
            problems.append("control row %s returned model %s" % (r["item_id"], r.get("model_returned")))
    if problems:
        return {"status": "WITHHELD", "problems": problems}
    fa = sum(1 for i in by_cat["wrong"] if jr[i["item_id"]]["verdict"])
    fr = sum(1 for i in by_cat["paraphrase"] if not jr[i["item_id"]]["verdict"])
    agree = sum(1 for i in by_cat["borderline"] if jr[i["item_id"]]["verdict"] == ar[i["item_id"]]["verdict"])
    ctl_rej = sum(1 for r in cr.values() if not r["verdict"])
    fa_u, fr_u, ag_l = wilson(fa, N_WRONG)[1], wilson(fr, N_PARA)[1], wilson(agree, N_BORDER)[0]
    b = bars
    res = {"status": "SCORED",
           "wrong_false_accept": {"k": fa, "n": N_WRONG, "rate": fa / N_WRONG, "wilson_upper": fa_u, "bar_upper_at_most": b["fa_upper_max"], "pass": fa_u <= b["fa_upper_max"]},
           "correct_false_reject": {"k": fr, "n": N_PARA, "rate": fr / N_PARA, "wilson_upper": fr_u, "bar_upper_at_most": b["fr_upper_max"], "pass": fr_u <= b["fr_upper_max"]},
           "audit_agreement": {"k": agree, "n": N_BORDER, "rate": agree / N_BORDER, "wilson_lower": ag_l, "bar_lower_at_least": b["agree_lower_min"], "pass": ag_l >= b["agree_lower_min"],
                               "label": "DEVIATION: agent audit, not human; agreement is against a second model (Codex)"},
           "control_shuffled_gold": {"rejected": ctl_rej, "n": N_CONTROL, "bar_rejected_at_least": b["control_rejected_min"], "pass": ctl_rej >= b["control_rejected_min"]},
           "disagree_with_construction": [{"item_id": i["item_id"], "construction": i["construction_label"], "judge": jr[i["item_id"]]["verdict"]}
                                         for c in ("wrong", "paraphrase") for i in by_cat[c] if (i["construction_label"] == "yes") != jr[i["item_id"]]["verdict"]],
           "borderline_by_kind": {}}
    for i in by_cat["borderline"]:
        k = res["borderline_by_kind"].setdefault(i["kind"], {"n": 0, "judge_yes": 0, "audit_yes": 0, "agree": 0})
        k["n"] += 1
        k["judge_yes"] += int(jr[i["item_id"]]["verdict"])
        k["audit_yes"] += int(ar[i["item_id"]]["verdict"])
        k["agree"] += int(jr[i["item_id"]]["verdict"] == ar[i["item_id"]]["verdict"])
    res["g_judge_pass"] = all(res[k]["pass"] for k in ("wrong_false_accept", "correct_false_reject", "audit_agreement", "control_shuffled_gold"))
    return res


def main(items_p, judge_p, control_p, audit_p, bars_p, out_p):
    res = score(json.load(open(items_p)), _rows(judge_p), _rows(control_p), _rows(audit_p), json.load(open(bars_p)))
    json.dump(res, open(out_p, "w"), indent=1)
    print(json.dumps({k: res[k] for k in res if k in ("status", "g_judge_pass", "problems", "wrong_false_accept", "correct_false_reject", "audit_agreement", "control_shuffled_gold")}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:7])
