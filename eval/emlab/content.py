"""Content identity (review amendment A): the dataset holds the SAME conversation under different session ids
(measured by the independent review: 19,195 ids but 18,464 distinct contents; 399 of 940 gold ids have a
byte-identical twin). "Same session" therefore means "same content hash" wherever a rule says so.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Sequence

from .dataset import Question


def session_hashes(questions: Iterable[Question]) -> Dict[str, str]:
    """session id -> sha256 of the ordered (role, text) list of its turns. Raises if an id maps to two contents."""
    per: Dict[str, List[List[str]]] = {}
    for q in questions:
        seen = set()
        for t in q.turns:
            per.setdefault((q.qid, t.session_id), []).append([t.role, t.text])
    out: Dict[str, str] = {}
    for (qid, sid), turns in per.items():
        h = hashlib.sha256(json.dumps(turns, ensure_ascii=False).encode("utf-8")).hexdigest()
        if sid in out and out[sid] != h:
            raise ValueError("session id %s has two different contents" % sid)
        out[sid] = h
    return out


def content_clusters(questions: Sequence[Question], hashes: Dict[str, str]) -> Dict[str, str]:
    """qid -> content cluster id (smallest member qid). Union over: questions whose gold sessions share a content
    hash, and each base question with its `_abs` twin (identical gold turns, disjoint haystacks)."""
    qs = list(questions)
    parent = {q.qid: q.qid for q in qs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    owner: Dict[str, str] = {}
    for q in qs:
        for s in q.gold_session_ids:
            h = hashes.get(s)
            if h is None:
                continue
            if h in owner:
                union(owner[h], q.qid)
            else:
                owner[h] = q.qid
    ids = set(parent)
    for q in qs:
        if q.qid.endswith("_abs") and q.qid[:-4] in ids:
            union(q.qid[:-4], q.qid)
    return {q.qid: find(q.qid) for q in qs}
