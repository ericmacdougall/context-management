"""Stage X0 inputs (pilot; no model call): the 200 session files of the extraction canary, the seeded run order and the key.
The selection rule is in STAGE-X0.md (committed before this ran). Session files carry opaque ids and plain turns only (no has_answer flags, no
question or gold information); the key (group of each session) is written to a separate directory the runner must not read."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime
from typing import Any, Dict, List

from .anon import AnonMap, opaque_sid
from .build_rag_contexts import load_common
from .dataset import select_split

SEED = "16096:X0"
FMT = "%Y/%m/%d (%a) %H:%M"
LOW_CAP = 40  # cap on gold sessions of the low-overlap questions that are not weak-list questions (not binding: 33 exist)
N_TOTAL = 200


def quantiles(vals: List[float], qs=(0.0, 0.25, 0.5, 0.75, 0.9, 1.0)) -> Dict[str, float]:
    v = sorted(vals)
    return {("p%d" % round(100 * q)): float(v[min(len(v) - 1, int(math.floor(q * (len(v) - 1))))]) for q in qs}


def allocate(shares: List[float], total: int) -> List[int]:
    """Largest remainder allocation of `total` over bins with the given shares."""
    raw = [s * total for s in shares]
    base = [int(math.floor(x)) for x in raw]
    left = total - sum(base)
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))[:left]:
        base[i] += 1
    return base


def main(dataset, art, sets_path, out_root):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    H = ctx.hashes
    raw = json.load(open(dataset, encoding="utf-8"))
    # content -> turns, earliest date over ALL occurrences in the dataset (the single date rule), representative real id (smallest)
    turns_of: Dict[str, List[Dict[str, str]]] = {}
    date_of: Dict[str, datetime] = {}
    rep: Dict[str, str] = {}
    for rec in raw:
        for sid, d, s in zip(rec["haystack_session_ids"], rec["haystack_dates"], rec["haystack_sessions"]):
            h = H[sid]
            dt = datetime.strptime(d, FMT)
            if h not in date_of or dt < date_of[h]:
                date_of[h] = dt
            if h not in turns_of:
                turns_of[h] = [{"role": t["role"], "content": t["content"]} for t in s]
            if h not in rep or sid < rep[h]:
                rep[h] = sid
    chars = {h: sum(len(t["content"]) for t in tr) for h, tr in turns_of.items()}
    # groups
    anon = AnonMap(list(H))
    weak: List[str] = []
    for q in qs:
        tg = json.load(open(os.path.join(out_root, "analyst_tasks_g300", "H3", q.qid + ".json")))
        if not any(anon.real_rid(c["round_id"]).rsplit(":", 1)[0] in set(q.gold_session_ids) for c in tg["candidates"]):
            weak.append(q.qid)
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    qmap = {q.qid: q for q in qs}
    low_rest = sorted(x for x in low if x in qmap and x not in set(weak))
    ku = sorted(q.qid for q in qs if q.qtype == "knowledge-update" and not q.is_abstention)
    group: Dict[str, str] = {}
    qs_of: Dict[str, List[str]] = {}

    def add(qids: List[str], g: str, cap: int = 10 ** 9):
        n = 0
        for x in qids:
            for gid in sorted(qmap[x].gold_session_ids):
                h = H[gid]
                qs_of.setdefault(h, [])
                if x not in qs_of[h]:
                    qs_of[h].append(x)
                if h not in group:
                    if n >= cap:
                        continue
                    group[h] = g
                    n += 1

    add(sorted(weak), "weak_list")
    add(low_rest, "low_overlap", LOW_CAP)
    add(ku, "knowledge_update")
    # random non-gold sample, stratified by session length (chars) with the whole-pool length quintiles as strata
    allgold = {H[g] for q in all_qs for g in q.gold_session_ids if g in H}
    pil = select_split(all_qs, "pilot")
    cand = sorted({H[t.session_id] for q in pil for t in q.turns} - allgold)
    pool_chars = sorted(chars.values())
    edges = [pool_chars[int(len(pool_chars) * i / 5.0)] for i in range(1, 5)]
    binof = lambda h: sum(1 for e in edges if chars[h] > e)
    n_rand = N_TOTAL - len(group)
    quota = allocate([0.2] * 5, n_rand)
    rng = random.Random(SEED + ":sample")
    sample: List[str] = []
    for b in range(5):
        members = sorted(h for h in cand if binof(h) == b)
        rng.shuffle(members)
        sample += members[:quota[b]]
        assert len(members) >= quota[b]
    for h in sample:
        group[h] = "random_non_gold"
    assert len(group) == N_TOTAL, len(group)
    # session files
    sdir = os.path.join(out_root, "stage_x0", "sessions")
    kdir = os.path.join(out_root, "stage_x0_key")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    key_rows, files = [], {}
    for h in sorted(group):
        o = opaque_sid(rep[h])
        assert o not in files, "opaque id collision"
        files[o] = h
        d = date_of[h].strftime(FMT)
        json.dump({"session_id": o, "date": d, "turns": turns_of[h]}, open(os.path.join(sdir, o + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        key_rows.append({"opaque_session_id": o, "group": group[h], "real_session_id": rep[h], "content_hash": h, "questions": qs_of.get(h, []),
                         "chars": chars[h], "turns": len(turns_of[h]), "date": d})
    order = sorted(files, key=lambda o: hashlib.sha256((SEED + ":order:" + o).encode()).hexdigest())
    json.dump({"seed": SEED + ":order", "rule": "sorted by sha256(seed:order:opaque id); gold and non-gold interleave; the first 10 are a random 10 of the 200",
               "order": order}, open(os.path.join(out_root, "stage_x0", "run_order.json"), "w"), indent=1)
    json.dump({"weak_list_questions": sorted(weak), "low_overlap_other_questions": low_rest, "knowledge_update_questions": ku, "sessions": key_rows},
              open(os.path.join(kdir, "selection_key.json"), "w"), indent=1)
    # report (no gold membership per session)
    rnd = [chars[h] for h in sample]
    gold_h = [h for h in group if group[h] != "random_non_gold"]
    rep_out = {"n_sessions": len(group), "n_gold_sessions": len(gold_h), "n_random": len(sample), "low_overlap_cap": LOW_CAP,
               "weak_list_questions": len(weak), "gold_sessions_by_group": {g: sum(1 for h in group if group[h] == g) for g in ("weak_list", "low_overlap", "knowledge_update")},
               "length_chars": {"whole_pool_18464": quantiles(list(chars.values())), "pilot_non_gold_candidates": quantiles([chars[h] for h in cand]),
                                "random_sample": quantiles(rnd), "gold_sessions": quantiles([chars[h] for h in gold_h]), "all_200": quantiles([chars[h] for h in group])},
               "strata_edges_chars": edges, "stratum_quota": quota, "random_sample_by_stratum": [sum(1 for h in sample if binof(h) == b) for b in range(5)],
               "pool_n": len(chars), "candidate_n": len(cand), "mean_chars": {"pool": sum(chars.values()) / len(chars), "random_sample": sum(rnd) / len(rnd)},
               "date_rule": "earliest date over all occurrences of the content in the dataset (strptime %Y/%m/%d (%a) %H:%M)"}
    json.dump(rep_out, open(os.path.join(out_root, "stage_x0", "prep_report.json"), "w"), indent=1)
    print(json.dumps(rep_out))


if __name__ == "__main__":
    main(*sys.argv[1:5])
