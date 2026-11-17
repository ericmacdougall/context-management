"""Post-hoc exploratory analysis of the extraction outputs already on disk (Stage X0, X0B, X0C). No model call. Rules: STAGE-X0C.md Section 9
(written before this ran). Outputs a JSON of counts plus a reading file (facts to be read by the eval owner, kept outside the repository)."""
from __future__ import annotations

import itertools
import json
import os
import random
import re
import sys
from collections import Counter
from typing import Any, Dict, List

from . import provenance_x04 as prov
from .stage_x0b_score import quote_match

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def pnorm(s: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", str(s).lower())).strip()


def union_dedup(draws: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Greedy union of fact lists in order, a fact is new iff its quote matches no fact already in the union. -> sizes after each draw and added per draw."""
    uni: List[Dict[str, Any]] = []
    added = []
    for facts in draws:
        n0 = len(uni)
        for f in facts:
            if not any(quote_match(f.get("quote", ""), g.get("quote", "")) for g in uni):
                uni.append(f)
        added.append(len(uni) - n0)
    return {"union": uni, "added": added, "sizes": list(itertools.accumulate(added))}


def only_in_one(a: List[Dict[str, Any]], b: List[Dict[str, Any]]):
    """-> (facts of a with no match in b, facts of b with no match in a, facts of a with a match in b, facts of b with a match in a)."""
    m = lambda x, y: any(quote_match(x.get("quote", ""), z.get("quote", "")) for z in y)
    return [f for f in a if not m(f, b)], [f for f in b if not m(f, a)], [f for f in a if m(f, b)], [f for f in b if m(f, a)]


def speaker_of(quote: str, groups, idx: int) -> Dict[str, Any]:
    ok, sp = prov.anchor(quote, groups, idx)
    return {"verbatim": ok, "speakers": sorted(sp)}


def classify_ungrounded(quote: str, groups, idx: int) -> str:
    """For a quote not verbatim in its cited round: 'other_round' (verbatim in another round of the session), 'punct_cited' (equal after removing
    punctuation, or all ellipsis-separated parts found in order, in the cited round), 'punct_other' (the same in another round), 'paraphrase'
    (token coverage of at least 0.8 in the best round), 'ungrounded' (none of these)."""
    q = prov.norm(quote)
    for i, g in enumerate(groups):
        if i != idx and q and q in prov.norm(" ".join(t["content"] for t in g)):
            return "other_round"
    parts = [pnorm(p) for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote) if len(pnorm(p)) > 3] or ([pnorm(quote)] if pnorm(quote) else [])

    def parts_in(text: str) -> bool:
        pos = 0
        for p in parts:
            j = text.find(p, pos)
            if j < 0:
                return False
            pos = j + len(p)
        return bool(parts)
    texts = [pnorm(" ".join(t["content"] for t in g)) for g in groups]
    if isinstance(idx, int) and 0 <= idx < len(groups) and parts_in(texts[idx]):
        return "punct_cited"
    if any(parts_in(t) for i, t in enumerate(texts) if i != idx):
        return "punct_other"
    toks = set(re.findall(r"[a-z0-9']+", q))
    best = max((len(toks & set(re.findall(r"[a-z0-9']+", t))) / float(len(toks) or 1) for t in texts), default=0.0)
    return "paraphrase" if best >= 0.8 else "ungrounded"


def main(out_root, in_root, out_json, reading_path):
    key = json.load(open(os.path.join(out_root, "stage_x0c_key", "selection_key.json")))
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    rows0 = {r["opaque_session_id"]: r for r in k0["sessions"]}
    reps = [r["opaque_session_id"] for r in key["sessions"] if r["has_prior_cap30"]]
    turns = {}

    def sess_groups(o):
        if o not in turns:
            turns[o] = prov.rounds_of_turns(json.load(open(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), encoding="utf-8"))["turns"])
        return turns[o]
    L = lambda p: json.load(open(p, encoding="utf-8"))["facts"]
    res: Dict[str, Any] = {"exploratory": True, "post_hoc": True}
    reading: List[str] = []

    # ---------- (1) what a second pass buys; union curve over 1, 2, 3 draws (X0B run, arm S run, arm P run; the third draw is a prompt with one extra sentence)
    per, curve_orders = {}, []
    for o in reps:
        d1, d2, d3 = L(os.path.join(in_root, "stage_x0b", o + ".json")), L(os.path.join(in_root, "stage_x0c", "S", o + ".json")), L(os.path.join(in_root, "stage_x0c", "P", o + ".json"))
        u12 = union_dedup([d1, d2])
        u123 = union_dedup([d1, d2, d3])
        rr = [prov.anchor(f.get("quote", ""), sess_groups(o), f.get("round")) for f in u12["union"]]
        per[o] = {"run1_X0B": len(d1), "run2_S": len(d2), "union_1_2": len(u12["union"]), "added_by_run2": u12["added"][1], "run3_P": len(d3), "union_1_2_3": len(u123["union"]),
                  "added_by_P_after_two": u123["added"][2], "union_1_2_quote_pass": sum(1 for ok, _ in rr if ok), "group": rows0[o]["group"], "questions": rows0[o]["questions"]}
        for order in itertools.permutations(range(3)):
            seq = union_dedup([[d1, d2, d3][i] for i in order])
            curve_orders.append((o, order, seq["added"]))
    tot = lambda k: sum(v[k] for v in per.values())
    res["second_pass"] = {"per_session": per, "run1_total": tot("run1_X0B"), "run2_total": tot("run2_S"), "union_total": tot("union_1_2"), "added_by_second_run": tot("added_by_run2"),
                          "union_quote_pass": tot("union_1_2_quote_pass"), "union_1_2_3_total": tot("union_1_2_3"), "added_by_third_draw": tot("added_by_P_after_two")}
    sums = {k: [0.0, 0] for k in (1, 2, 3)}
    for o, order, added in curve_orders:
        for k in (1, 2, 3):
            sums[k][0] += added[k - 1]
            sums[k][1] += 1
    res["union_curve_mean_over_orders"] = {"mean_new_facts_per_session_by_draw_index": {str(k): v[0] / v[1] for k, v in sums.items()},
                                           "note": "mean over all 6 orders of the three draws and over the 10 sessions; draw 1 is the first run's size, draws 2 and 3 are the facts they add; the third draw is arm P (a prompt with one extra sentence), not an identical-prompt run"}
    # reading file: gold sessions among the replicate sessions, facts present in only one of the two identical-prompt runs
    reading.append("POST-HOC READING FILE (pilot gold only; not for any test-split use). Facts present in only one of the two identical-prompt cap-30 runs.")
    D = {r["question_id"]: r for r in json.load(open(os.path.join(out_root, "..", "data", "longmemeval_s_cleaned.json"), encoding="utf-8"))}
    for o in reps:
        if rows0[o]["group"] == "random_non_gold":
            continue
        d1, d2 = L(os.path.join(in_root, "stage_x0b", o + ".json")), L(os.path.join(in_root, "stage_x0c", "S", o + ".json"))
        o1, o2, _, _ = only_in_one(d1, d2)
        for q in rows0[o]["questions"]:
            reading.append("=" * 90)
            reading.append("SESSION %s | QUESTION %s (%s): %s | ANSWER: %s" % (o, q, D[q]["question_type"], D[q]["question"], str(D[q]["answer"])[:400]))
        reading.append("  ONLY IN RUN 1 (X0B), %d facts:" % len(o1))
        reading += ["    r%s | %s | %s | %s" % (f.get("round"), f.get("subject"), f.get("predicate"), f.get("object")) for f in o1]
        reading.append("  ONLY IN RUN 2 (arm S), %d facts:" % len(o2))
        reading += ["    r%s | %s | %s | %s" % (f.get("round"), f.get("subject"), f.get("predicate"), f.get("object")) for f in o2]

    # ---------- (2) unstable (only in one of two runs) against stable facts
    stab, unst = [], []
    for o in reps:
        d1, d2 = L(os.path.join(in_root, "stage_x0b", o + ".json")), L(os.path.join(in_root, "stage_x0c", "S", o + ".json"))
        a1, a2, b1, b2 = only_in_one(d1, d2)
        n = len(sess_groups(o))
        for f in a1 + a2:
            unst.append((o, f, n))
        for f in b1 + b2:
            stab.append((o, f, n))

    def prof(items):
        c = Counter()
        a = 0
        for o, f, n in items:
            g = sess_groups(o)
            ok, sp = prov.anchor(f.get("quote", ""), g, f.get("round"))
            a += 1 if ok else 0
            au = f.get("author", "__missing__")
            c["author_" + ("user" if au == "user" else "assistant" if au == "assistant" else "missing")] += 1
            r = f.get("round")
            c["third_%d" % (min(2, (r * 3) // max(1, n)) if isinstance(r, int) else -1)] += 1
        return {"n": len(items), "quote_pass": a, "quote_pass_rate": (a / float(len(items))) if items else None, "counts": dict(c)}
    res["unstable_vs_stable"] = {"only_in_one_run": prof(unst), "in_both_runs": prof(stab),
                                 "note": "facts of both runs pooled over the 10 replicate sessions; position = cited round in thirds of the session (0 first, 2 last); author = the model's author field"}

    # ---------- (3) mechanical author and quote repair over all extraction outputs so far
    outs = []
    for o in rows0:
        outs.append(("X0", o, L(os.path.join(in_root, "stage_x0", o + ".json"))))
    for o in reps:
        outs.append(("X0B", o, L(os.path.join(in_root, "stage_x0b", o + ".json"))))
    for arm in ("S", "P"):
        for o in key["sessions"]:
            outs.append(("X0C_" + arm, o["opaque_session_id"], L(os.path.join(in_root, "stage_x0c", arm, o["opaque_session_id"] + ".json"))))
    agree, mism, nodef, missing_author = 0, [], 0, 0
    verb, unver = 0, Counter()
    for src, o, facts in outs:
        g = sess_groups(o)
        for f in facts:
            q = f.get("quote", "")
            idx = f.get("round")
            ok, sp = prov.anchor(q, g, idx)
            if not ok:
                unver[classify_ungrounded(q, g, idx)] += 1
                continue
            verb += 1
            if len(sp) != 1:
                nodef += 1
                continue
            mech = list(sp)[0]
            au = f.get("author")
            if au is None:
                missing_author += 1
            elif au == mech:
                agree += 1
            else:
                mism.append({"src": src, "sid": o, "round": idx, "model_author": au, "speaker": mech, "quote": q, "fact": "%s | %s | %s" % (f.get("subject"), f.get("predicate"), f.get("object"))})
    total = sum(len(f) for _, _, f in outs)
    res["mechanical_repair"] = {"outputs": len(outs), "facts_total": total, "verbatim_in_cited_round": verb, "not_verbatim": sum(unver.values()),
                               "author_defined_by_speaker": agree + len(mism) + missing_author, "author_agreement": agree, "author_mismatch": len(mism), "author_field_missing": missing_author,
                               "verbatim_but_speaker_ambiguous_or_split": nodef, "not_verbatim_classified": dict(unver),
                               "reading": "up to 20 author mismatches are in the reading file"}
    rng = random.Random(16096)
    sample = rng.sample(mism, min(20, len(mism)))
    reading.append("=" * 90)
    reading.append("AUTHOR MISMATCHES (model author field against the speaker of the round where the quote is found): %d in total; a seeded random %d" % (len(mism), len(sample)))
    for m in sample:
        g = sess_groups(m["sid"])
        reading.append("  [%s %s r%s] model=%s speaker=%s | fact: %s" % (m["src"], m["sid"], m["round"], m["model_author"], m["speaker"], m["fact"]))
        reading.append("      quote: %s" % m["quote"])
        for t in g[m["round"]]:
            reading.append("      %s: %s" % (t["role"], t["content"][:260].replace("\n", " ")))
    json.dump(res, open(out_json, "w"), indent=1)
    open(reading_path, "w", encoding="utf-8").write("\n".join(reading))
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:5])
