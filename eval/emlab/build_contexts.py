"""Build answer-level pilot contexts (RAG, FACTS, FACTS+RAG) at H0 and H1 for every pilot question. No LLM.

  embed   embed fact text with the same E5 model (passage prefix) -> facts_vectors.npy   (torch; t2-dev venv)
  index   create/fill the second index emlab-facts-v1 on the node (modest bulk)
  build   write contexts to <out>/contexts/<arm>/<level>/<qid>.json plus a metrics file
Dates: own-haystack sessions use THIS question's date; D500 distractor sessions have no date in this question, so
they use the date of their first appearance (lowest question id whose haystack contains them) and are flagged
`date_source: first_appearance` (registered rule; may fall after the question date, flagged per item).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .context import ARMS, context_metrics, pack_facts, pack_mix, pack_rag, write_context
from .corpus import cluster_ids
from .dataset import LongMemEvalLoader, select_split
from .facts import FactsRetriever, load_facts, load_question_cues
from .live import ExactRetriever
from .pool import cluster_haystacks, level_session_ids, pool_universe, with_sessions
from .retrievers import run_arm

LEVELS = ("H0", "H1")


def embed_facts(facts_path, model_dir, out_dir, threads=12):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(threads)
    name = "intfloat/multilingual-e5-small"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=model_dir)
    model = AutoModel.from_pretrained(name, cache_dir=model_dir).eval()
    facts = load_facts(facts_path)
    texts = ["passage: " + f["text"] + (" | " + " ".join(f["cue_keys"]) if f["cue_keys"] else "") for f in facts]
    vecs = []
    for i in range(0, len(texts), 64):
        enc = tok(texts[i:i + 64], padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            h = model(**enc).last_hidden_state
        m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
        vecs.append(torch.nn.functional.normalize((h * m).sum(1) / m.sum(1), dim=-1).numpy())
    np.save(os.path.join(out_dir, "facts_vectors.npy"), np.concatenate(vecs).astype("float32"))
    return len(facts)


def build(dataset, art, facts_path, cues_path, sets_path, out_root, budget):
    import numpy as np
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(all_qs, "pilot")
    sets = json.load(open(sets_path))
    cl = cluster_ids(all_qs)
    ch = cluster_haystacks(all_qs, cl)
    uni = pool_universe(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    texts = {}
    for line in open(a + "rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    facts = load_facts(facts_path)
    fvec = np.load(os.path.join(os.path.dirname(facts_path), "facts_vectors.npy")) if os.path.exists(
        os.path.join(os.path.dirname(facts_path), "facts_vectors.npy")) else None
    fr = FactsRetriever(facts, fvec)
    fmap = {f["id"]: f for f in facts}
    cues = load_question_cues(cues_path)
    qvec = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    first_date = {}
    for q in sorted(all_qs, key=lambda x: x.qid):
        for t in q.turns:
            first_date.setdefault(t.session_id, t.date.strftime("%Y-%m-%d"))
    metrics = []
    for q in qs:
        own_dates = {t.session_id: t.date.strftime("%Y-%m-%d") for t in q.turns}
        for lv in LEVELS:
            ids = level_session_ids(q, uni, ch[cl[q.qid]], lv, sets)
            dates = {s: own_dates.get(s) or first_date[s] for s in ids}
            pq = with_sessions(q, ids)
            ranked_r = run_arm(ex, pq, "rrf", 400, "off", rrf_k=60)
            ranked_f = fr.rank(q.question, cues.get(q.qid, []), qvec[q.qid], ids, 400)
            ranked_f = [f for f in ranked_f if "%s:%s" % (fmap[f]["session_id"], fmap[f]["round_index"]) in texts]
            packs = {"rag": pack_rag(ranked_r, dates, texts, budget),
                     "facts": pack_facts(ranked_f, fmap, dates, texts, budget),
                     "facts_rag": pack_mix(ranked_f, ranked_r, fmap, dates, texts, budget)}
            for arm in ARMS:
                items = packs[arm]
                for it in items:
                    it["date_source"] = "question" if it["session_id"] in own_dates else "first_appearance"
                    it["after_question_date"] = it["date"] > q.question_date.strftime("%Y-%m-%d")
                write_context(out_root, arm, lv, q, budget, items)
                m = context_metrics(q, items)
                m.update({"qid": q.qid, "qtype": q.qtype, "arm": arm, "level": lv, "abstention": q.is_abstention})
                metrics.append(m)
    json.dump({"budget_tokens": budget, "metrics": metrics}, open(os.path.join(out_root, "context_metrics.json"), "w"), indent=1)
    return metrics


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["embed", "build"], required=True)
    ap.add_argument("--dataset-path")
    ap.add_argument("--artifacts")
    ap.add_argument("--facts")
    ap.add_argument("--cues")
    ap.add_argument("--sets")
    ap.add_argument("--model-dir")
    ap.add_argument("--out")
    ap.add_argument("--budget", type=int)
    a = ap.parse_args(argv)
    if a.action == "embed":
        print("embedded", embed_facts(a.facts, a.model_dir, os.path.dirname(a.facts)))
    else:
        m = build(a.dataset_path, a.artifacts, a.facts, a.cues, a.sets, a.out, a.budget)
        print("contexts", len(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
