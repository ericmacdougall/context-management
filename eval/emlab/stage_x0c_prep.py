"""Stage X0C inputs (pilot; no model call): 20 sessions, run twice (arm S identical prompt, arm P preference line), 40 jobs.
Rule in STAGE-X0C.md (committed before this ran). Session files are copies of the Stage X0 files (opaque ids, no gold flags); the key is separate."""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sys

SEED = "16096:X0C"
REPLICATE = "x0c-s-1"  # replicate id of arm S (in the runner cache key, so no earlier cap-30 output can be served)


def main(dataset, out_root, in_root):
    D = {r["question_id"]: r for r in json.load(open(dataset, encoding="utf-8"))}
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    kb = json.load(open(os.path.join(out_root, "stage_x0b_key", "selection_key.json")))
    prep0 = json.load(open(os.path.join(out_root, "stage_x0", "prep_report.json")))
    edges = prep0["strata_edges_chars"]
    binof = lambda c: sum(1 for e in edges if c > e)
    rows = {r["opaque_session_id"]: r for r in k0["sessions"]}
    in_b = [r["opaque_session_id"] for r in kb["sessions"]]
    chosen, why = list(in_b), {o: "X0B session (prior cap-30 output exists)" for o in in_b}
    # (a) every other gold session of a single-session-preference or single-session-user question in the X0 set
    qtype = lambda q: D[q]["question_type"]
    pref = sorted(o for o, r in rows.items() if o not in in_b and any(qtype(q) in ("single-session-preference", "single-session-user") for q in r["questions"]))
    for o in pref:
        chosen.append(o)
        why[o] = "preference or single-session-user gold session"
    # (b) random non-gold sessions of the X0 random sample not in X0B: one per length stratum 1 to 5 plus one more from stratum 4 (index 3)
    pool = {b: sorted(o for o, r in rows.items() if r["group"] == "random_non_gold" and o not in in_b and binof(r["chars"]) == b) for b in range(5)}
    rng = random.Random(SEED + ":nongold")
    for b in (0, 1, 2, 3, 4, 3):
        cand = [o for o in pool[b] if o not in chosen]
        rng.shuffle(cand)
        chosen.append(cand[0])
        why[cand[0]] = "random non-gold, length stratum %d" % (b + 1)
    chosen = chosen[:20]
    assert len(chosen) == 20 and len(set(chosen)) == 20, len(chosen)
    sdir = os.path.join(out_root, "stage_x0c", "sessions")
    kdir = os.path.join(out_root, "stage_x0c_key")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    for o in chosen:
        shutil.copy(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), os.path.join(sdir, o + ".json"))
    order = sorted(chosen, key=lambda o: hashlib.sha256((SEED + ":order:" + o).encode()).hexdigest())
    jobs = []
    for i, o in enumerate(order):
        arms = ("S", "P") if i % 2 == 0 else ("P", "S")  # interleaved session by session; the arm that goes first alternates
        for a_ in arms:
            jobs.append({"order_index": len(jobs), "session_id": o, "arm": a_, "replicate": REPLICATE if a_ == "S" else None})
    json.dump({"seed": SEED + ":order", "replicate_id_arm_S": REPLICATE,
               "rule": "20 sessions sorted by sha256(seed:order:id); for each session both arms run back to back, S first for even positions and P first for odd positions, so drift over the run affects both arms equally",
               "order": order, "jobs": jobs}, open(os.path.join(out_root, "stage_x0c", "run_order.json"), "w"), indent=1)
    json.dump({"sessions": [{"opaque_session_id": o, "group": rows[o]["group"], "questions": rows[o]["questions"], "question_types": sorted({qtype(q) for q in rows[o]["questions"]}),
                             "chars": rows[o]["chars"], "why": why[o], "has_prior_cap30": o in in_b} for o in chosen]},
              open(os.path.join(kdir, "selection_key.json"), "w"), indent=1)
    print(json.dumps({"n": len(chosen), "x0b": len(in_b), "pref_or_ssu": len(pref), "random": 6, "why": {o: why[o] for o in chosen}}))


if __name__ == "__main__":
    main(*sys.argv[1:4])
