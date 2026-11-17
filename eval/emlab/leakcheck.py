"""Do retrieval inputs carry label-bearing strings? Scans every indexed/embedded chunk text and every question text
for the guard pattern and for real session ids (token match). Structural; prints counts only. New."""
import json
import re
import sys

from .dataset import LongMemEvalLoader
from .guard import PATTERN


def main(dataset, chunks_path):
    qs = list(LongMemEvalLoader(dataset).questions())
    sids = {s for q in qs for s in q.haystack_session_ids}
    tok = re.compile(r"[A-Za-z0-9_]+")
    pat_hits = id_hits = n = 0
    for line in open(chunks_path, encoding="utf-8"):
        t = json.loads(line)["text"]
        n += 1
        if PATTERN.search(t):
            pat_hits += 1
        if any(w in sids for w in tok.findall(t)):
            id_hits += 1
    qp = sum(1 for q in qs if PATTERN.search(q.question))
    qi = sum(1 for q in qs if any(w in sids for w in tok.findall(q.question)))
    print(json.dumps({"chunks_scanned": n, "chunks_with_label_pattern": pat_hits, "chunks_containing_a_real_session_id": id_hits,
                      "questions_scanned": len(qs), "questions_with_label_pattern": qp, "questions_containing_a_real_session_id": qi}))


if __name__ == "__main__":
    main(*sys.argv[1:3])
