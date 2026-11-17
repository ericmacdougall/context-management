"""Stage X0B inputs (pilot; no model call): 10 of the 56 sessions that hit the 12-fact cap in Stage X0, for a re-extraction at cap 30.
Rule in STAGE-X0B.md (committed before this ran). Session files are copies of the Stage X0 files (opaque ids, no gold flags); the key is separate."""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sys

SEED = "16096:X0B"
FORCED_QUESTIONS = ("fca70973", "gpt4_ab202e7f")
CAP = 12


def main(out_root, in_root):
    key = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    rows = {r["opaque_session_id"]: r for r in key["sessions"]}
    n_facts = {}
    for o in rows:
        n_facts[o] = len(json.load(open(os.path.join(in_root, "stage_x0", o + ".json")))["facts"])
    capped = sorted(o for o in rows if n_facts[o] >= CAP)
    forced = [o for o in capped if any(q in rows[o]["questions"] for q in FORCED_QUESTIONS)]
    rest_gold = sorted(o for o in capped if o not in forced and rows[o]["group"] != "random_non_gold")
    rest_non = sorted(o for o in capped if rows[o]["group"] == "random_non_gold")
    r1, r2 = random.Random(SEED + ":gold"), random.Random(SEED + ":nongold")
    r1.shuffle(rest_gold)
    r2.shuffle(rest_non)
    chosen = forced + rest_gold[:3] + rest_non[:3]
    assert len(chosen) == 10 and len(set(chosen)) == 10, (len(forced), len(chosen))
    sdir = os.path.join(out_root, "stage_x0b", "sessions")
    kdir = os.path.join(out_root, "stage_x0b_key")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    for o in chosen:
        shutil.copy(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), os.path.join(sdir, o + ".json"))
    order = sorted(chosen, key=lambda o: hashlib.sha256((SEED + ":order:" + o).encode()).hexdigest())
    json.dump({"seed": SEED + ":order", "order": order}, open(os.path.join(out_root, "stage_x0b", "run_order.json"), "w"), indent=1)
    json.dump({"capped_in_x0": len(capped), "sessions": [{"opaque_session_id": o, "group": rows[o]["group"], "questions": rows[o]["questions"], "x0_facts": n_facts[o],
                                                         "chars": rows[o]["chars"], "why": "forced" if o in forced else ("random gold" if rows[o]["group"] != "random_non_gold" else "random non-gold")} for o in chosen]},
              open(os.path.join(kdir, "selection_key.json"), "w"), indent=1)
    print(json.dumps({"capped_in_x0": len(capped), "forced": len(forced), "random_gold_pool": len(rest_gold), "random_non_gold_pool": len(rest_non), "order": order}))


if __name__ == "__main__":
    main(*sys.argv[1:3])
