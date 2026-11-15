import sys, random
sys.path.insert(0, ".")
from exp import c5_attack as c
rng = random.Random(3)
bad = 0
for _ in range(2000):
    evs, expect = c.gen_case(rng)
    out = c.pipeline(evs)
    v = c.violations(evs, expect, out)
    if v and bad < 2:
        bad += 1
        print(expect, v)
        for e in evs: print("  EV", e["actor"], e["trust_origin"], e["text"])
        for x in out: print("  OUT", x[0]["kind"], x[1], x[2], x[3], x[4])
