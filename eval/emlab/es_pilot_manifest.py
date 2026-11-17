"""Build the pilot manifest the ES-pilot scorer requires (ES-PILOT.md section 7).

  core     : on t2-dev, from the dataset file: the pilot split's questions that have retrieval gold (select_split(..., "pilot")), their content
             clusters (PoolContext.clusters, the same code every pilot stage uses), the levels, and cross-checks against files committed BEFORE the
             fresh ES-pilot run (results/pilot/curve_rerank_v2.json question ids, results/pilot/splits_v2.json pilot count).
  stamp    : on any host, add the sha256 of the evidence artifacts (rows.jsonl.gz, ce.jsonl) as committed.
The manifest is derived from the dataset and the split code, NOT from the run's rows; it authenticates the files against the commit, not the run."""
from __future__ import annotations

import hashlib
import json
import os
import sys

LEVELS = ["H0", "H1", "H2", "H3"]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def core(dataset: str, curve_rerank_json: str, splits_json: str, source_note: str) -> dict:
    from .dataset import LongMemEvalLoader, select_split
    from .pool import PoolContext
    all_qs = list(LongMemEvalLoader(dataset).questions())
    ctx = PoolContext(all_qs)
    qs = [q for q in select_split(all_qs, "pilot") if q.retrieval_gold_session_ids]
    qids = sorted(q.qid for q in qs)
    prior = sorted(r["qid"] for r in json.load(open(curve_rerank_json))["per_question"])
    split_n = json.load(open(splits_json))["pilot"]["n"]
    return {"version": 1, "source": source_note, "dataset_sha256": sha256_file(dataset), "qids": qids,
            "clusters": {q.qid: ctx.clusters[q.qid] for q in qs}, "levels": LEVELS, "arms": ["bm25", "dense", "rrf", "ce"],
            "n_questions": len(qids), "n_clusters": len({ctx.clusters[x] for x in qids}),
            "crosschecks": {"curve_rerank_v2_question_ids_equal": prior == qids, "curve_rerank_v2_n": len(prior), "splits_v2_pilot_n_including_abstention": split_n}}


def stamp(manifest_path: str, run_dir: str) -> None:
    m = json.load(open(manifest_path))
    m["artifacts"] = {n: sha256_file(os.path.join(run_dir, n)) for n in ("rows.jsonl.gz", "ce.jsonl")}
    json.dump(m, open(manifest_path, "w"), indent=1, sort_keys=True)


if __name__ == "__main__":
    if sys.argv[1] == "core":
        json.dump(core(*sys.argv[2:6]), open(sys.argv[6], "w"), indent=1, sort_keys=True)
    else:
        stamp(sys.argv[2], sys.argv[3])
