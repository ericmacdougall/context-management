"""Build answer-level pilot contexts (RAG, FACTS, FACTS+RAG) at H0 and H1 for every pilot question. No LLM.

  embed   embed fact text with the same E5 model (passage prefix) -> facts_vectors.npy   (torch; t2-dev venv)
  index   create/fill the second index emlab-facts-v1 on the node (modest bulk)
  build   write contexts to <out>/contexts/<arm>/<level>/<qid>.json plus a metrics file
Dates: own-haystack sessions use their date for THIS question; distractor sessions get a synthetic date (see
synthetic_date), flagged per item. event_time is shown only when the extractor reference date equals the used date.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .anon import opaque_rid, opaque_sid
from .context import context_metrics, pack_facts, pack_mix, pack_rag, write_context
from .dataset import LongMemEvalLoader, select_split
from .facts import FactsRetriever, load_facts, load_question_cues
from .live import ExactRetriever
from .pool import PoolContext, with_sessions
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


def synthetic_date(qid: str, sid: str, earliest: datetime, qdate: datetime, seed: int = 16096) -> datetime:
    """Distractor date for THIS question: uniform between the question's earliest own-haystack date and the
    question date, seeded by sha256("<seed>:<qid>:<sid>"); deterministic, order-independent, never after the
    question date (day granularity plus a time of day)."""
    h = int(hashlib.sha256(("%d:%s:%s" % (seed, qid, sid)).encode()).hexdigest()[:12], 16) / float(16 ** 12)
    span = max(0, int((qdate - earliest).total_seconds()))
    return earliest + timedelta(seconds=int(h * span))


FMT = "%Y-%m-%d %H:%M"
REF_KEYS = ("ref_date", "reference_date", "session_date")  # extractor reference-date field: to confirm


def event_time_allowed(fact: Dict[str, Any], used_date: str, synthetic: bool) -> Optional[bool]:
    """Show event_time only when the extractor's reference date equals the session date used for THIS question.
    None = the fact carries no reference date (treated as not shown and counted separately)."""
    ref = next((fact[k] for k in REF_KEYS if fact.get(k)), None)
    if ref is None:
        return None
    return (not synthetic) and str(ref)[:10] == used_date[:10]


def build(dataset, art, facts_path, cues_path, sets_path, out_root, budget, lowoverlap_path=None):
    import numpy as np
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(all_qs, "pilot")
    sets = json.load(open(sets_path))
    ctx = PoolContext(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    texts = {}
    for line in open(a + "rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    facts = load_facts(facts_path)
    vp = os.path.join(os.path.dirname(facts_path), "facts_vectors.npy")
    fvec = np.load(vp) if os.path.exists(vp) else None
    fr = FactsRetriever(facts, fvec)
    fmap = {f["id"]: f for f in facts}
    cues = load_question_cues(cues_path)
    qvec = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    low = set()
    if lowoverlap_path:
        low = set(json.load(open(lowoverlap_path))["thresholds"]["100"]["qids"])
    stats = {"facts_shown": 0, "event_time_dropped": 0, "event_time_kept": 0, "no_reference_date": 0}
    metrics = []
    for q in qs:
        own = {}
        for t in q.turns:
            own.setdefault(t.session_id, t.date)
        earliest = min(own.values())
        for lv in LEVELS:
            ids = ctx.level_sessions(q, lv, sets)
            syn = {s for s in ids if s not in own}
            dts = {s: (own[s] if s in own else synthetic_date(q.qid, s, earliest, q.question_date)) for s in ids}
            dates = {s: d.strftime(FMT) for s, d in dts.items()}
            pq = with_sessions(q, ids)
            ranked_r = run_arm(ex, pq, "rrf", 400, "off", rrf_k=60)
            ranked_f = fr.rank(q.question, cues.get(q.qid, []), qvec[q.qid], ids, 400)
            ranked_n = fr.rank(q.question, cues.get(q.qid, []), qvec[q.qid], ids, 400, use_cue=False)
            ok_round = lambda fid: "%s:%s" % (fmap[fid]["session_id"], fmap[fid]["round_index"]) in texts
            ranked_f = [f for f in ranked_f if ok_round(f)]
            ranked_n = [f for f in ranked_n if ok_round(f)]
            local = {}
            for fid in set(ranked_f) | set(ranked_n):
                f = dict(fmap[fid])
                ok = event_time_allowed(f, dates[f["session_id"]], f["session_id"] in syn)
                if f.get("event_time") not in (None, ""):
                    if ok is None:
                        stats["no_reference_date"] += 1
                    elif ok:
                        stats["event_time_kept"] += 1
                    if not ok:
                        f["event_time"] = None
                        stats["event_time_dropped"] += 1
                local[fid] = f
            packs = {"rag": pack_rag(ranked_r, dates, texts, budget),
                     "facts": pack_facts(ranked_f, local, dates, texts, budget),
                     "facts_rag": pack_mix(ranked_f, ranked_r, local, dates, texts, budget),
                     "facts_nocue": pack_facts(ranked_n, local, dates, texts, budget)}
            for arm, items in packs.items():
                for it in items:
                    it["synthetic_date"] = it["session_id"] in syn
                    it["after_question_date"] = it["date"] > q.question_date.strftime(FMT)
                m = context_metrics(q, items)  # computed on REAL ids before anonymisation
                if arm != "facts_nocue":  # diagnostic arm: retrieval level only, no context file for a reader
                    anon_items = [dict(i, item_id=("src:" if i["kind"] == "source_round" else "") + opaque_rid(i["round_id"])
                                       if i["kind"] != "fact" else i["item_id"], round_id=opaque_rid(i["round_id"]),
                                       session_id=opaque_sid(i["session_id"])) for i in items]
                    write_context(out_root, arm, lv, q, budget, anon_items, set(ctx.hashes), {x.qid for x in all_qs})
                m.update({"qid": q.qid, "qtype": q.qtype, "arm": arm, "level": lv, "abstention": q.is_abstention,
                          "low_overlap_h3": q.qid in low})
                metrics.append(m)
    json.dump({"budget_tokens": budget, "event_time_stats": stats, "metrics": metrics},
              open(os.path.join(out_root, "context_metrics.json"), "w"), indent=1)
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
    ap.add_argument("--lowoverlap")
    a = ap.parse_args(argv)
    if a.action == "embed":
        print("embedded", embed_facts(a.facts, a.model_dir, os.path.dirname(a.facts)))
    else:
        m = build(a.dataset_path, a.artifacts, a.facts, a.cues, a.sets, a.out, a.budget, a.lowoverlap)
        print("contexts", len(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
