"""Facts loading and exact retrieval over facts (hybrid: BM25 on fact text, dense on fact text vectors,
lexical match on cue keys, fused by RRF 60). Placeholder field names, documented: id, session_id, round_index,
text, slot, cardinality, modality, value, event_time, cue_keys (list of strings), quote. Verify against the
extractor owner's file when it arrives."""
from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set

from .fusion import rrf
from .retrievers import tokens


def load_facts(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                f = json.loads(line)
                f.setdefault("cue_keys", [])
                if "round_index" not in f and "round" in f:  # extractor field name (verified by main)
                    f["round_index"] = f["round"]
                out.append(f)
    return out


def load_question_cues(path: str) -> Dict[str, List[str]]:
    """Placeholder format: one JSON object per line {"question_id": ..., "cue_keys": [...]}."""
    out: Dict[str, List[str]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                out[str(r["question_id"])] = list(r["cue_keys"])
    return out


class Bm25:
    def __init__(self, docs: Sequence[Sequence[str]]):
        self.docs = [Counter(d) for d in docs]
        self.dl = [len(d) for d in docs]
        self.n = len(docs)
        self.avg = sum(self.dl) / max(1, self.n)
        self.df: Counter = Counter()
        for d in self.docs:
            self.df.update(d.keys())

    def score(self, query: Sequence[str], idx: Sequence[int]) -> Dict[int, float]:
        out: Dict[int, float] = {}
        for i in idx:
            tf = self.docs[i]
            s = 0.0
            for w in set(query):
                if w in tf:
                    df = self.df[w]
                    idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                    s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * self.dl[i] / max(self.avg, 1e-9)))
            if s > 0:
                out[i] = s
        return out


class FactsRetriever:
    def __init__(self, facts: List[Dict[str, Any]], vectors=None):
        self.facts = facts
        self.vectors = vectors  # numpy array aligned with facts, optional (None skips the dense leg)
        self.text = Bm25([tokens(f["text"]) for f in facts])
        self.cue = Bm25([tokens(" ".join(f["cue_keys"])) for f in facts])
        self.by_session: Dict[str, List[int]] = {}
        for i, f in enumerate(facts):
            self.by_session.setdefault(f["session_id"], []).append(i)

    def rank(self, question: str, cue_keys: Sequence[str], qvec, sessions: Sequence[str], depth: int = 200) -> List[str]:
        idx = [i for s in sessions for i in self.by_session.get(s, ())]
        if not idx:
            return []
        legs: List[List[str]] = []
        sc = self.text.score(tokens(question), idx)
        legs.append([self.facts[i]["id"] for i, _ in sorted(sc.items(), key=lambda kv: (-kv[1], self.facts[kv[0]]["id"]))][:depth])
        if self.vectors is not None and qvec is not None:
            sims = self.vectors[idx] @ qvec
            order = sorted(range(len(idx)), key=lambda j: (-float(sims[j]), self.facts[idx[j]]["id"]))
            legs.append([self.facts[idx[j]]["id"] for j in order][:depth])
        cq = tokens(" ".join(cue_keys)) + tokens(question)
        sc = self.cue.score(cq, idx)
        legs.append([self.facts[i]["id"] for i, _ in sorted(sc.items(), key=lambda kv: (-kv[1], self.facts[kv[0]]["id"]))][:depth])
        return [d for d, _ in rrf([l for l in legs if l], k=60)][:depth]
