"""Stage 11 input: the PILOT questions with retrieval gold (94), as plain records (id, type, question, gold answer, date, content cluster).

Run on t2-dev against the read-only dataset with the eval loader on PYTHONPATH. The split is derived by the registered rule
(`select_split(..., "pilot")`) and cross-checked against the 94 task files of Stage G; only pilot rows are written, and the
function asserts that no other id is in the output. The test split is parsed in memory by the loader (as in every earlier stage)
but its rows are never inspected, printed or written.
usage: python extract_pilot_qa.py DATASET GOLD_IDS_DIR OUT.json
"""
import json
import os
import sys

from emlab.content import content_clusters, session_hashes
from emlab.dataset import LongMemEvalLoader, full_split_assignment


def main(dataset, ids_dir, out):
    qs = list(LongMemEvalLoader(dataset).questions())
    sp = full_split_assignment(qs)
    expected = sorted(f[:-5] for f in os.listdir(ids_dir) if f.endswith(".json"))
    pilot = sorted(q.qid for q in qs if sp[q.qid] == "pilot" and q.retrieval_gold_session_ids)
    assert pilot == expected, (len(pilot), len(expected))
    cc = content_clusters(qs, session_hashes(qs))
    rows = [{"qid": q.qid, "qtype": q.qtype, "question": q.question, "answer": q.answer,
             "question_date": q.question_date.strftime("%Y-%m-%d %H:%M"), "cluster": cc[q.qid]}
            for q in qs if q.qid in set(pilot)]
    assert len(rows) == 94 and all(sp[r["qid"]] == "pilot" for r in rows)
    json.dump(sorted(rows, key=lambda r: r["qid"]), open(out, "w"), indent=1)
    print("wrote", len(rows), "pilot rows;", len({r["cluster"] for r in rows}), "clusters")


if __name__ == "__main__":
    main(*sys.argv[1:4])
