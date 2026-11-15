"""Fixed micro-pilot rule (stated before any content was seen): from the eval loader's pilot split take, for each of
the five largest question types, the first two question ids in sorted order. Prints ids and types only."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"))
from emlab.dataset import LongMemEvalLoader, select_split

TYPES = ["multi-session", "temporal-reasoning", "knowledge-update", "single-session-user", "single-session-assistant"]


def pick(path):
    qs = list(LongMemEvalLoader(path).questions())
    pilot = select_split(qs, "pilot")
    out = []
    for t in TYPES:
        ids = sorted(q.qid for q in pilot if q.qtype == t)
        out += [(i, t) for i in ids[:2]]
    return out, len(pilot)


if __name__ == "__main__":
    ids, n = pick(sys.argv[1])
    print("pilot size", n)
    for i, t in ids:
        print(i, t)
