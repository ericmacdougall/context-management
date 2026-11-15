"""Unique round corpus, dataset verification, and the registered chunk rule.

ONE shared corpus of unique rounds serves all questions; every search is filtered to the question's own
haystack_session_ids. Chunk rule (applied by embed.py with the E5 tokenizer): the text of a round is
tokenised; if it exceeds CHUNK_TOKENS tokens it is cut at token offsets into consecutive non-overlapping
windows of CHUNK_TOKENS tokens; each chunk is its own document; a round's score is the max over its chunks.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, Iterable, List

from .dataset import LongMemEvalLoader, Question, build_rounds

CHUNK_TOKENS = 480  # leaves room for "passage: " and special tokens under E5's 512 limit


def unique_rounds(questions: Iterable[Question]) -> Dict[str, Dict[str, Any]]:
    """round_id -> {round_id, session_id, text}. Raises if a session id maps to different content across
    questions. Dates are deliberately NOT stored: the same session id carries different dates in different
    questions (measured: 3,942 of 19,195 ids), so the as-of logic uses each question's own haystack_dates."""
    out: Dict[str, Dict[str, Any]] = {}
    for q in questions:
        for r in q.rounds:
            rec = {"round_id": r.round_id, "session_id": r.session_id, "text": r.text}
            old = out.get(r.round_id)
            if old is None:
                out[r.round_id] = rec
            elif old != rec:
                raise ValueError("session content differs across questions: %s" % r.round_id)
    return out


def verify_dataset(path: str) -> Dict[str, Any]:
    """Re-measure the facts the pre-registration relies on, straight from the file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rep: Dict[str, Any] = {"questions": len(data)}
    rep["types"] = dict(Counter(r["question_type"] for r in data))
    rep["abstention"] = sum(1 for r in data if str(r["question_id"]).endswith("_abs"))
    rep["abstention_with_gold_sessions"] = sum(1 for r in data if str(r["question_id"]).endswith("_abs") and r.get("answer_session_ids"))
    rep["answer_non_str"] = sum(1 for r in data if not isinstance(r["answer"], str))
    refs = [s for r in data for s in r["haystack_session_ids"]]
    rep["session_refs"], rep["unique_sessions"] = len(refs), len(set(refs))
    content: Dict[str, Any] = {}
    dates: Dict[str, str] = {}
    conflicts = date_conflicts = 0
    for r in data:
        for sid, d, s in zip(r["haystack_session_ids"], r["haystack_dates"], r["haystack_sessions"]):
            key = json.dumps(s, sort_keys=True)
            if sid in content and content[sid] != key:
                conflicts += 1
            content.setdefault(sid, key)
            if sid in dates and dates[sid] != d:
                date_conflicts += 1
            dates.setdefault(sid, d)
    rep["session_content_conflicts"], rep["session_ids_with_differing_dates_across_questions"] = conflicts, date_conflicts
    per_q = [len(r["haystack_session_ids"]) for r in data]
    per_q.sort()
    rep["sessions_per_question"] = {"min": per_q[0], "median": per_q[len(per_q) // 2], "max": per_q[-1]}
    # chronology, late sessions
    from .dataset import parse_date
    in_order = late_q = late_gold = 0
    late_by_type: Counter = Counter()
    for r in data:
        ds = [parse_date(d) for d in r["haystack_dates"]]
        if ds == sorted(ds):
            in_order += 1
        qd = parse_date(r["question_date"])
        late = [s for s, d in zip(r["haystack_session_ids"], ds) if d > qd]
        if late:
            late_q += 1
            late_by_type[r["question_type"]] += 1
            late_gold += sum(1 for s in late if s in set(r.get("answer_session_ids", [])))
    rep["haystack_in_chronological_order"] = in_order
    rep["questions_with_late_sessions"], rep["late_by_type"], rep["late_sessions_that_are_gold"] = late_q, dict(late_by_type), late_gold
    owners: Dict[str, set] = {}
    for r in data:
        for s in r["haystack_session_ids"]:
            owners.setdefault(s, set()).add(r["question_id"])
    leak = sum(1 for r in data for s in r.get("answer_session_ids", []) if len(owners.get(s, ())) > 1)
    rep["answer_sessions_in_other_haystacks"] = leak
    rep["distinct_answer_session_ids_shared_across_questions"] = len({s for r in data for s in r.get("answer_session_ids", []) if len(owners.get(s, ())) > 1})
    from .dataset import LongMemEvalLoader as _L
    qs = [_L.parse_record(r) for r in data]
    cl = cluster_ids(qs)
    rep["clusters"] = len(set(cl.values()))
    rep["multi_question_clusters"] = sum(1 for c, n in Counter(cl.values()).items() if n > 1)
    ku = [q for q in qs if q.qtype == "knowledge-update" and not q.is_abstention]
    ok = 0
    for q in ku:
        latest = q.gold_latest_session_ids
        ok += 1 if latest and latest[0] == max(q.gold_session_ids, key=lambda s: s.rsplit("_", 1)[-1]) else 0
    rep["ku_nonabs"], rep["ku_latest_by_date_equals_highest_suffix"] = len(ku), ok
    rep["non_abstention_without_has_answer_turn"] = sum(
        1 for q in qs if not q.is_abstention and not q.gold_round_ids)
    # evidence structure per type
    ev: Dict[str, Dict[str, Any]] = {}
    for r in data:
        t = r["question_type"]
        hs = sum(1 for sess in r["haystack_sessions"] for turn in sess if turn.get("has_answer"))
        e = ev.setdefault(t, {"n": 0, "no_has_answer": 0, "answer_sessions": Counter(), "has_answer_turns": Counter()})
        e["n"] += 1
        e["no_has_answer"] += 1 if hs == 0 else 0
        e["answer_sessions"][len(r.get("answer_session_ids", []))] += 1
        e["has_answer_turns"][hs] += 1
    rep["evidence_by_type"] = {t: {"n": e["n"], "questions_without_has_answer_turn": e["no_has_answer"],
                                   "answer_session_count_hist": dict(sorted(e["answer_sessions"].items())),
                                   "has_answer_turn_count_hist": dict(sorted(e["has_answer_turns"].items()))}
                               for t, e in ev.items()}
    return rep


def cluster_ids(questions: Iterable[Question]) -> Dict[str, str]:
    """qid -> cluster id (smallest qid in the cluster). Questions that share a gold session are one
    cluster (union-find), so a conversation cannot sit in both pilot and test and is one unit in the bootstrap."""
    qs = list(questions)
    parent = {q.qid: q.qid for q in qs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: Dict[str, str] = {}
    for q in qs:
        for s in q.gold_session_ids:
            if s in owner:
                a, b = find(owner[s]), find(q.qid)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[s] = q.qid
    return {q.qid: find(q.qid) for q in qs}


def write_rounds(questions: Iterable[Question], path: str) -> int:
    rs = unique_rounds(questions)
    with open(path, "w", encoding="utf-8") as fh:
        for rid in sorted(rs):
            fh.write(json.dumps(rs[rid]) + "\n")
    return len(rs)


def load_all(path: str) -> List[Question]:
    return list(LongMemEvalLoader(path).questions())
