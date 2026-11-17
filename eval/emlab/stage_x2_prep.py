"""Stage X2 inputs (pilot; no model call): the gold sessions of every pilot question that are not yet in the fact pool (one session per call) and, with the
calls that remain, a cap-30 re-extraction of the gold sessions that are in the pool at cap 12. Rule in STAGE-X2.md (committed before this ran).
Session files carry opaque ids and plain turns only; the key (kind, questions) is in a separate directory the runner must not read. Everything stays on t2-dev."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from typing import Dict, List

from .anon import opaque_sid
from .build_rag_contexts import load_common

SEED = "16096:X2"
FMT = "%Y/%m/%d (%a) %H:%M"
CAP_CALLS = 250


def main(dataset, art, sets_path, out_root, in_root):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    H = ctx.hashes
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    k1 = json.load(open(os.path.join(out_root, "stage_x1_key", "selection_key.json")))
    pool = {r["content_hash"] for r in k0["sessions"]} | {r["content_hash"] for r in k1["sessions"]}
    need: Dict[str, List[str]] = {}
    for q in qs:
        for g in q.gold_session_ids:
            if H[g] not in pool:
                need.setdefault(H[g], [])
                if q.qid not in need[H[g]]:
                    need[H[g]].append(q.qid)
    raw = json.load(open(dataset, encoding="utf-8"))
    turns_of, date_of, rep = {}, {}, {}
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
    n_new = len(need)
    assert n_new <= CAP_CALLS, "rule needed: more missing gold sessions than the cap"
    # re-extraction group: gold sessions of Stage X0 at cap 12 without a cap-30 output (only if calls remain)
    c12 = []
    for r in k0["sessions"]:
        if r["group"] == "random_non_gold":
            continue
        o = r["opaque_session_id"]
        if os.path.exists(os.path.join(in_root, "stage_x0b", o + ".json")) or os.path.exists(os.path.join(in_root, "stage_x0c", "S", o + ".json")):
            continue
        if len(json.load(open(os.path.join(in_root, "stage_x0", o + ".json")))["facts"]) >= 12:
            c12.append(r)
    c12 = c12[:max(0, CAP_CALLS - n_new)]
    sdir = os.path.join(out_root, "stage_x2", "sessions")
    kdir = os.path.join(out_root, "stage_x2_key")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    rows = []
    for h in sorted(need):
        o = opaque_sid(rep[h])
        json.dump({"session_id": o, "date": date_of[h].strftime(FMT), "turns": turns_of[h]}, open(os.path.join(sdir, o + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        rows.append({"opaque_session_id": o, "kind": "new_gold", "questions": need[h], "content_hash": h, "chars": sum(len(t["content"]) for t in turns_of[h])})
    for r in c12:
        o = r["opaque_session_id"]
        shutil.copy(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), os.path.join(sdir, o + ".json"))
        rows.append({"opaque_session_id": o, "kind": "reextract_cap30", "questions": r["questions"], "content_hash": r["content_hash"], "chars": r["chars"]})
    assert len({r["opaque_session_id"] for r in rows}) == len(rows), "opaque id collision"
    order = sorted((r["opaque_session_id"] for r in rows), key=lambda o: hashlib.sha256((SEED + ":order:" + o).encode()).hexdigest())
    json.dump({"seed": SEED + ":order", "rule": "sorted by sha256(seed:order:opaque id); new and re-extracted sessions interleave", "order": order}, open(os.path.join(out_root, "stage_x2", "run_order.json"), "w"), indent=1)
    json.dump({"sessions": rows}, open(os.path.join(kdir, "selection_key.json"), "w"), indent=1)
    rep_out = {"calls": len(rows), "new_gold_sessions": n_new, "reextract_cap30": len(c12), "questions_with_new_gold": len({q for v in need.values() for q in v}),
               "questions_all_gold_already_in_pool": sum(1 for q in qs if all(H[g] in pool for g in q.gold_session_ids)), "pilot_questions_with_gold": len(qs)}
    json.dump(rep_out, open(os.path.join(out_root, "stage_x2", "prep_report.json"), "w"), indent=1)
    print(json.dumps(rep_out))


if __name__ == "__main__":
    main(*sys.argv[1:6])
