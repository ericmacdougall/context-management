"""Dataset loader interface.

LongMemEvalLoader parses the raw LongMemEval JSON layout AS RECALLED (ASSUMPTION; every field name is
"to confirm from the dataset card"). The fixture under fixtures/ is hand-written in that same layout so
the parser is exercised, but it proves nothing about the real files.
"""
from __future__ import annotations

import abc
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})(?:\s*\([A-Za-z]{3}\))?\s*(\d{2}):(\d{2})")


def parse_date(s: str) -> datetime:
    """Parse '2023/05/20 (Sat) 02:21' (format to confirm from the dataset card) or ISO 8601."""
    m = _DATE_RE.search(s)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi)
    return datetime.fromisoformat(s)


@dataclass
class Turn:
    turn_id: str
    session_id: str
    index: int
    role: str
    text: str
    has_answer: bool
    date: datetime


@dataclass
class Question:
    qid: str
    qtype: str
    question: str
    answer: str
    question_date: datetime
    turns: List[Turn]
    gold_session_ids: List[str]
    gold_turn_ids: List[str]
    stale_session_ids: List[str] = field(default_factory=list)  # our own annotation, not in LongMemEval
    expected_low_overlap: Optional[bool] = None

    @property
    def is_abstention(self) -> bool:
        return self.qid.endswith("_abs")  # convention to confirm from the dataset card

    @property
    def session_ids(self) -> List[str]:
        seen: List[str] = []
        for t in self.turns:
            if t.session_id not in seen:
                seen.append(t.session_id)
        return seen


class DatasetLoader(abc.ABC):
    @abc.abstractmethod
    def questions(self) -> Iterator[Question]:
        ...


class LongMemEvalLoader(DatasetLoader):
    """Parses a list of records with: question_id, question_type, question, answer, question_date,
    haystack_session_ids, haystack_dates, haystack_sessions[[{role, content, has_answer?}]], answer_session_ids."""

    def __init__(self, path: str):
        self.path = path

    def questions(self) -> Iterator[Question]:
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for rec in data:
            yield self.parse_record(rec)

    @staticmethod
    def parse_record(rec: dict) -> Question:
        sids = rec["haystack_session_ids"]
        dates = rec["haystack_dates"]
        sessions = rec["haystack_sessions"]
        if not (len(sids) == len(dates) == len(sessions)):
            raise ValueError("haystack arrays differ in length for %s" % rec.get("question_id"))
        turns: List[Turn] = []
        for sid, dstr, sess in zip(sids, dates, sessions):
            d = parse_date(dstr)
            for i, t in enumerate(sess):
                turns.append(Turn("%s:%d" % (sid, i), sid, i, t["role"], t["content"],
                                  bool(t.get("has_answer", False)), d))
        gold_sessions = list(rec.get("answer_session_ids", []))
        gold_turns = [t.turn_id for t in turns if t.has_answer]
        return Question(
            qid=str(rec["question_id"]), qtype=rec["question_type"], question=rec["question"],
            answer=str(rec["answer"]), question_date=parse_date(rec["question_date"]), turns=turns,
            gold_session_ids=gold_sessions, gold_turn_ids=gold_turns,
            stale_session_ids=list(rec.get("x_stale_session_ids", [])),
            expected_low_overlap=rec.get("x_expected_low_overlap"),
        )


class FixtureLoader(LongMemEvalLoader):
    def __init__(self, path: Optional[str] = None):
        import os
        super().__init__(path or os.path.join(os.path.dirname(__file__), "..", "fixtures", "tiny.json"))


SPLIT_SEED = 16096  # fixed before any data is seen; see PREREGISTRATION.md


def split_pilot_test(qs: Sequence[Tuple[str, str]], seed: int = SPLIT_SEED,
                     pilot_fraction: float = 0.2) -> Dict[str, str]:
    """Deterministic stratified split by question id. qs = [(qid, stratum)] -> {qid: 'pilot'|'test'}.

    Within each stratum, ids are ordered by sha256("seed:qid") and the first ceil(fraction*n) become pilot
    (at least one when n >= 2). Independent of Python version and of input order.
    """
    by: Dict[str, List[str]] = {}
    for qid, stratum in qs:
        by.setdefault(stratum, []).append(qid)
    out: Dict[str, str] = {}
    for stratum, ids in by.items():
        ids = sorted(ids, key=lambda q: hashlib.sha256(("%d:%s" % (seed, q)).encode()).hexdigest())
        n_pilot = min(len(ids) - 1, max(1, math.ceil(pilot_fraction * len(ids)))) if len(ids) >= 2 else 0
        for i, q in enumerate(ids):
            out[q] = "pilot" if i < n_pilot else "test"
    return out
