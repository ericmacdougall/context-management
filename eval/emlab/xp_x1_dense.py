"""Post-hoc exploratory follow-up of Stage X1 (no model call except the LOCAL E5-small embedding model on CPU): dense and fused retrieval over the repaired
1,000-session fact pool and over its raw rounds. Rules and the "worth designing a new test" criteria: STAGE-X1.md Section 8 (written before this ran)."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # the local checkout has no numpy; the run is on t2-dev
    np = None

from . import provenance_x04 as prov
from .retrievers import tokens
from .stage_x0_score import bm25_rank
from .stage_x1_score import kept, repair_fact, stmt

RRF_K = 60
CHUNK_TOKENS = 480
ARMS = ("bm25_facts", "dense_facts", "rrf_facts", "bm25_raw", "dense_raw", "rrf_raw", "fused_facts_raw")


def rrf_merge(lists: Sequence[Sequence[Any]], k: int = RRF_K) -> List[Any]:
    """Reciprocal rank fusion of ranked lists of hashable ids; ties broken by first list, then id order of appearance."""
    score: Dict[Any, float] = {}
    first: Dict[Any, Tuple[int, int]] = {}
    for li, lst in enumerate(lists):
        for r, d in enumerate(lst):
            score[d] = score.get(d, 0.0) + 1.0 / (k + r + 1)
            first.setdefault(d, (li, r))
    return sorted(score, key=lambda d: (-score[d], first[d]))


def first_rank(order: Sequence[Any], owner, gold: set) -> Optional[int]:
    for i, d in enumerate(order):
        if owner(d) in gold:
            return i + 1
    return None


def session_order(order: Sequence[Any], owner) -> List[str]:
    seen, out = set(), []
    for d in order:
        s = owner(d)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


class Embedder:
    def __init__(self, model_dir: str, threads: int = 12):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ["OMP_NUM_THREADS"] = str(threads)
        import torch
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(threads)
        self.torch = torch
        name = "intfloat/multilingual-e5-small"
        self.tok = AutoTokenizer.from_pretrained(name, cache_dir=model_dir)
        self.model = AutoModel.from_pretrained(name, cache_dir=model_dir).eval()

    def embed(self, texts: List[str], batch: int = 32) -> np.ndarray:
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), 384), dtype="float32")
        for s in range(0, len(order), batch):
            idx = order[s:s + batch]
            enc = self.tok([texts[i] for i in idx], padding=True, truncation=True, max_length=512, return_tensors="pt")
            with self.torch.inference_mode():
                h = self.model(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
            v = (h * m).sum(1) / m.sum(1)
            out[idx] = self.torch.nn.functional.normalize(v, dim=-1).numpy()
        return out

    def chunks(self, text: str) -> List[str]:
        enc = self.tok(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False, verbose=False)
        offs = enc["offset_mapping"]
        if len(offs) <= CHUNK_TOKENS:
            return [text]
        spans = []
        for i in range(0, len(offs), CHUNK_TOKENS):
            w = offs[i:i + CHUNK_TOKENS]
            spans.append((w[0][0] if i else 0, w[-1][1] if i + CHUNK_TOKENS < len(offs) else len(text)))
        return [text[s:e] for s, e in spans]


def main(dataset, art, sets_path, out_root, in_root, model_dir, out_json):
    from .build_rag_contexts import load_common
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qmap = {q.qid: q for q in qs}
    L = lambda p: json.load(open(p, encoding="utf-8"))
    k0 = L(os.path.join(out_root, "stage_x0_key", "selection_key.json"))
    k1 = L(os.path.join(out_root, "stage_x1_key", "selection_key.json"))
    rows0 = {r["opaque_session_id"]: r for r in k0["sessions"]}
    rows1 = {r["opaque_session_id"]: r for r in k1["sessions"]}
    turns = {o: L(os.path.join(out_root, "stage_x0", "sessions", o + ".json"))["turns"] for o in rows0}
    turns.update({o: L(os.path.join(out_root, "stage_x1", "sessions", o + ".json"))["turns"] for o in rows1})
    groups = {o: prov.rounds_of_turns(t) for o, t in turns.items()}
    # --- the pool exactly as in stage_x1_score (cap-30 output where one exists for an X0 session, else cap 12; repaired)
    cap30: Dict[str, List[Dict[str, Any]]] = {}
    for o in rows0:
        p = os.path.join(in_root, "stage_x0b", o + ".json")
        if os.path.exists(p):
            cap30[o] = L(p)["facts"]
    for o in rows0:
        p = os.path.join(in_root, "stage_x0c", "S", o + ".json")
        if o not in cap30 and os.path.exists(p):
            cap30[o] = L(p)["facts"]
    raw_facts = []
    for o in rows0:
        raw_facts += [dict(f, session_id=o) for f in (cap30[o] if o in cap30 else L(os.path.join(in_root, "stage_x0", o + ".json"))["facts"])]
    for o in rows1:
        raw_facts += [dict(f, session_id=o) for f in L(os.path.join(in_root, "stage_x1", o + ".json"))["facts"]]
    facts = [f for f in (repair_fact(f, groups[f["session_id"]]) for f in raw_facts) if kept(f)]
    rounds = [(o, i, " ".join("%s: %s" % (t["role"], t["content"]) for t in g)) for o in groups for i, g in enumerate(groups[o])]
    # --- queries
    weak = k0["weak_list_questions"]
    low_other = k0["low_overlap_other_questions"]
    ku = k0["knowledge_update_questions"]
    allq = weak + low_other + ku
    gold = {x: {o for o, r in rows0.items() if x in r["questions"]} for x in allq}
    emb = Embedder(model_dir)
    qv = {x: v for x, v in zip(allq, emb.embed(["query: " + qmap[x].question for x in allq]))}
    fv = emb.embed(["passage: " + " ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object")) for f in facts])
    ch_text, ch_round = [], []
    for ri, (_, _, text) in enumerate(rounds):
        for c in emb.chunks(text):
            ch_text.append("passage: " + c)
            ch_round.append(ri)
    cv = emb.embed(ch_text)
    ch_round = np.array(ch_round)
    f_owner = np.array([f["session_id"] for f in facts])
    r_owner = np.array([o for o, _, _ in rounds])
    f_tok = [stmt(f) for f in facts]
    r_tok = [tokens(t) for _, _, t in rounds]
    res: Dict[str, Any] = {"exploratory": True, "post_hoc": True, "pool": {"facts": len(facts), "rounds": len(rounds), "chunks": len(ch_text)}, "arms": list(ARMS)}

    def rankings(text_q: str, qvec: np.ndarray, ids: set):
        fi = np.array([i for i in range(len(facts)) if f_owner[i] in ids])
        ri = np.array([i for i in range(len(rounds)) if r_owner[i] in ids])
        out = {}
        b = bm25_rank([f_tok[i] for i in fi], tokens(text_q))
        out["bm25_facts"] = [("f", int(fi[j])) for j in b]
        s = fv[fi] @ qvec
        out["dense_facts"] = [("f", int(fi[j])) for j in np.argsort(-s, kind="stable")]
        out["rrf_facts"] = rrf_merge([out["bm25_facts"], out["dense_facts"]])
        b = bm25_rank([r_tok[i] for i in ri], tokens(text_q))
        out["bm25_raw"] = [("r", int(ri[j])) for j in b]
        cs = cv @ qvec
        rs = np.full(len(rounds), -9.0, dtype="float32")
        np.maximum.at(rs, ch_round, cs)
        sr = rs[ri]
        out["dense_raw"] = [("r", int(ri[j])) for j in np.argsort(-sr, kind="stable")]
        out["rrf_raw"] = rrf_merge([out["bm25_raw"], out["dense_raw"]])
        out["fused_facts_raw"] = rrf_merge([out["rrf_facts"], out["rrf_raw"]])
        return out

    owner = lambda d: (f_owner[d[1]] if d[0] == "f" else r_owner[d[1]])

    def run(ids: set, wrong: bool = False) -> Dict[str, Any]:
        per = {}
        for qi, x in enumerate(allq):
            qtext, qvec = qmap[x].question, qv[x]
            if wrong:
                if x not in weak:
                    continue
                y = weak[(weak.index(x) + 1) % len(weak)]
                qtext, qvec = qmap[y].question, qv[y]
            rk = rankings(qtext, qvec, ids)
            e = {"type": qmap[x].qtype}
            for a, order in rk.items():
                fr = first_rank(order, owner, gold[x])
                so = session_order(order, owner)
                sr = next((i + 1 for i, s in enumerate(so) if s in gold[x]), None)
                e[a] = {"first_gold_rank": fr, "top10": bool(fr and fr <= 10), "top50": bool(fr and fr <= 50), "gold_session_rank": sr, "sessions_in_list": len(so)}
            per[x] = e
        return per

    def tally(per, group) -> Dict[str, Any]:
        o = {}
        for a in ARMS:
            ranks = sorted(per[x][a]["first_gold_rank"] for x in group if per[x][a]["first_gold_rank"])
            sranks = sorted(per[x][a]["gold_session_rank"] for x in group if per[x][a]["gold_session_rank"])
            q = lambda v, p: v[min(len(v) - 1, int(p * (len(v) - 1)))] if v else None
            o[a] = {"n": len(group), "top10": sum(1 for x in group if per[x][a]["top10"]), "top50": sum(1 for x in group if per[x][a]["top50"]),
                    "first_gold_rank_median": q(ranks, 0.5), "first_gold_rank_q25_q75": [q(ranks, 0.25), q(ranks, 0.75)], "gold_session_rank_median": q(sranks, 0.5),
                    "gold_session_top10": sum(1 for x in group if per[x][a]["gold_session_rank"] and per[x][a]["gold_session_rank"] <= 10),
                    "gold_session_top50": sum(1 for x in group if per[x][a]["gold_session_rank"] and per[x][a]["gold_session_rank"] <= 50)}
        return o

    all_ids = set(rows0) | set(rows1)
    toy_ids = set(rows0)
    for label, ids in (("pool_1000", all_ids), ("toy_200", toy_ids)):
        per = run(ids)
        res[label] = {"weak_list": tally(per, weak), "low_overlap_other": tally(per, low_other), "knowledge_update": tally(per, ku), "per_question": per}
        w = run(ids, wrong=True)
        res[label]["wrong_query_control_weak"] = {a: {"top10": sum(1 for x in weak if w[x][a]["top10"]), "top50": sum(1 for x in weak if w[x][a]["top50"])} for a in ARMS}
    json.dump(res, open(out_json, "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:8])
