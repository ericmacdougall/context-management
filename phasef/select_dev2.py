"""Fresh gate set dev2 (main's rule): first 16 knowledge-update question ids in sorted order from the TEST split that are not abstention
questions and whose cluster has exactly one question. Prints ids only; no question text is read or printed."""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"))
from emlab.corpus import cluster_ids  # noqa: E402
from emlab.dataset import LongMemEvalLoader, select_split  # noqa: E402


def main(path, out):
    qs = list(LongMemEvalLoader(path).questions())
    cl = cluster_ids(qs)
    size = Counter(cl.values())
    test = select_split(qs, "test")
    ids = sorted(q.qid for q in test if q.qtype == "knowledge-update" and not q.is_abstention and size[cl[q.qid]] == 1)
    json.dump(ids[:16], open(out, "w"))
    print(len(ids), "eligible; first 16:", ids[:16])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
