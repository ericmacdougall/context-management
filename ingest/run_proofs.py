"""python3 -m ingest.run_proofs OUTDIR item [item...]   (items: 1 1b 2a 2b 2c 2d 2bctl 3 4 5 6). Run from a clean committed tree."""
import json
import os
import sys

from . import proof_items_123 as p123

REG = {"1": p123.item1, "1b": p123.item1b, "2a": p123.item2_delivery, "2b": p123.item2_race_threads, "2c": p123.item2_race_procs,
       "2d": p123.item2_zombie, "2bctl": lambda o: p123.item2_race_threads(o, fence=False, unique=False, tag="item2b_control_prereg_3layers"),
       "2bctl4": lambda o: p123.item2_race_threads(o, fence=False, unique=False, state_guard=False, tag="item2b_control_4layers"), "3": p123.item3}
try:
    from . import proof_items_456 as p456
    REG.update({"4": p456.item4, "5": p456.item5, "6": p456.item6, "7": p456.item7_probe})
except ImportError:
    pass

if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for it in sys.argv[2:]:
        s = REG[it](out)
        print(it, json.dumps(s, sort_keys=True)[:1500], flush=True)
