"""Structural diagnostics that read no retrieval result: as-of rule effect and date-parser coverage."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from .dataset import Question, visible_as_of
from .retrievers import allowed_sessions, session_dates
from .timeparse import parse_question_range


def asof_report(questions: List[Question]) -> Dict[str, Any]:
    tot_sessions = dropped = gold_dropped = gold_total = 0
    q_with_drop = q_gold_drop = 0
    strict_gold_dropped = 0
    by_type: Counter = Counter()
    for q in questions:
        dates = session_dates(q)
        d_here = {s for s, d in dates.items() if not visible_as_of(d, q.question_date)}
        strict_drop = {s for s, d in dates.items() if d > q.question_date}
        tot_sessions += len(dates)
        dropped += len(d_here)
        gs = set(q.retrieval_gold_session_ids)
        gold_total += len(gs)
        gd = len(gs & d_here)
        gold_dropped += gd
        strict_gold_dropped += len(gs & strict_drop)
        if d_here:
            q_with_drop += 1
            by_type[q.qtype] += 1
        if gd:
            q_gold_drop += 1
    return {"questions": len(questions), "sessions": tot_sessions,
            "sessions_removed_by_asof_day_rule": dropped,
            "fraction_sessions_removed": dropped / max(1, tot_sessions),
            "questions_where_asof_removes_any": q_with_drop, "by_type": dict(by_type),
            "gold_sessions": gold_total, "gold_sessions_removed_by_day_rule": gold_dropped,
            "questions_losing_gold_to_day_rule": q_gold_drop,
            "gold_sessions_removed_by_strict_timestamp_rule": strict_gold_dropped}


def parser_report(questions: List[Question]) -> Dict[str, Any]:
    n = len(questions)
    parsed = differs_filter = 0
    by_type: Counter = Counter()
    for q in questions:
        if parse_question_range(q.question, q.question_date) is None:
            continue
        parsed += 1
        by_type[q.qtype] += 1
        a, _ = allowed_sessions(q, "asof")
        f, _ = allowed_sessions(q, "expand_filter")
        if f != a:
            differs_filter += 1
    return {"questions": n, "range_parsed": parsed, "fraction_parsed": parsed / max(1, n),
            "expand_filter_differs_from_asof": differs_filter, "parsed_by_type": dict(by_type)}
