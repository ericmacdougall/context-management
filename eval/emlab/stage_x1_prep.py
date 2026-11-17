"""Stage X1 inputs (pilot; no model call): 800 further pilot-haystack sessions for one-pass extraction (cap 30). Rule in STAGE-X1.md (committed
before this ran). Session files carry opaque ids and plain turns only; the key (stratum, long flag) is in a separate directory the runner must not read."""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
from datetime import datetime
from typing import Dict, List

from .anon import opaque_sid
from .build_rag_contexts import load_common
from .dataset import select_split

SEED = "16096:X1"
FMT = "%Y/%m/%d (%a) %H:%M"
N_LONG = 40
N_RANDOM_PER_STRATUM = 152  # 5 strata x 152 = 760 random sessions


def main(dataset, art, sets_path, out_root):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    H = ctx.hashes
    raw = json.load(open(dataset, encoding="utf-8"))
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
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    prep0 = json.load(open(os.path.join(out_root, "stage_x0", "prep_report.json")))
    edges = prep0["strata_edges_chars"]
    binof = lambda c: sum(1 for e in edges if c > e)
    x0 = {r["content_hash"] for r in k0["sessions"]}
    allgold = {H[g] for q in all_qs for g in q.gold_session_ids if g in H}
    pil = select_split(all_qs, "pilot")
    cand = sorted({H[t.session_id] for q in pil for t in q.turns} - allgold - x0)
    longest = sorted(cand, key=lambda h: (-chars[h], h))[:N_LONG]
    rest = [h for h in cand if h not in set(longest)]
    rng = random.Random(SEED + ":sample")
    chosen: Dict[str, str] = {h: "long" for h in longest}
    quota_check = []
    for b in range(5):
        members = sorted(h for h in rest if binof(chars[h]) == b)
        rng.shuffle(members)
        assert len(members) >= N_RANDOM_PER_STRATUM, (b, len(members))
        for h in members[:N_RANDOM_PER_STRATUM]:
            chosen[h] = "random"
        quota_check.append(len(members))
    assert len(chosen) == N_LONG + 5 * N_RANDOM_PER_STRATUM == 800, len(chosen)
    sdir = os.path.join(out_root, "stage_x1", "sessions")
    kdir = os.path.join(out_root, "stage_x1_key")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    files, key_rows = {}, []
    for h in sorted(chosen):
        o = opaque_sid(rep[h])
        assert o not in files, "opaque id collision"
        files[o] = h
        json.dump({"session_id": o, "date": date_of[h].strftime(FMT), "turns": turns_of[h]}, open(os.path.join(sdir, o + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        key_rows.append({"opaque_session_id": o, "kind": chosen[h], "stratum": binof(chars[h]) + 1, "chars": chars[h], "turns": len(turns_of[h]), "content_hash": h})
    order = sorted(files, key=lambda o: hashlib.sha256((SEED + ":order:" + o).encode()).hexdigest())
    json.dump({"seed": SEED + ":order", "rule": "sorted by sha256(seed:order:opaque id); long, random and all strata interleave", "order": order}, open(os.path.join(out_root, "stage_x1", "run_order.json"), "w"), indent=1)
    json.dump({"sessions": key_rows}, open(os.path.join(kdir, "selection_key.json"), "w"), indent=1)
    rl = sorted(chars[h] for h in longest)
    rep_out = {"n": 800, "n_long": N_LONG, "n_random": 760, "random_per_stratum": N_RANDOM_PER_STRATUM, "candidates": len(cand), "candidates_per_stratum_after_long_removed": quota_check,
               "long_chars": {"min": rl[0], "median": rl[len(rl) // 2], "max": rl[-1]},
               "random_chars_mean": sum(chars[h] for h in chosen if chosen[h] == "random") / 760.0, "whole_pool_mean_chars": sum(chars.values()) / len(chars),
               "strata_edges_chars": edges, "date_rule": "earliest date over all occurrences of the content in the dataset"}
    json.dump(rep_out, open(os.path.join(out_root, "stage_x1", "prep_report.json"), "w"), indent=1)
    print(json.dumps(rep_out))


if __name__ == "__main__":
    main(*sys.argv[1:5])
