"""Dataset loader interface for LongMemEval-S (cleaned): xiaowu0162/longmemeval-cleaned, MIT.

Facts measured on the file by a verifier and re-checked by `emlab.corpus.verify_dataset`:
500 questions; `answer` is sometimes an int (cast to str); abstention = question_id ending `_abs`
(no gold location); haystack_session_ids are NOT chronological; 76 questions hold sessions dated later
than question_date by under one day, so the as-of rule works at calendar-day granularity.
"""
from __future__ import annotations

import abc
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})(?:\s*\([A-Za-z]{3}\))?\s*(\d{2}):(\d{2})")


def parse_date(s: str) -> datetime:
    """Parse '2023/05/20 (Sat) 02:21' or ISO 8601."""
    m = _DATE_RE.search(s)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi)
    return datetime.fromisoformat(s)


def visible_as_of(session_date: datetime, question_date: datetime) -> bool:
    """Day-granularity as-of rule: a session is visible iff its calendar day <= the question's calendar day.

    A strict timestamp comparison would drop gold sessions dated hours after question_date (75 of 76 such
    questions in LongMemEval-S have the later session as gold).
    """
    return session_date.date() <= question_date.date()


def as_of_end_of_day(question_date: datetime) -> str:
    """Upper bound string for the Elasticsearch `session_date` range filter (format strict_date_hour_minute)."""
    return question_date.strftime("%Y-%m-%dT23:59")


@dataclass
class Turn:
    session_id: str
    index: int
    role: str
    text: str
    has_answer: bool
    date: datetime


@dataclass
class Round:
    """Index unit: a user message plus the assistant reply that follows it."""
    round_id: str  # "<session_id>:<round index>"
    session_id: str
    index: int
    text: str
    has_answer: bool
    date: datetime


def build_rounds(sid: str, turns: Sequence[Turn]) -> List[Round]:
    groups: List[List[Turn]] = []
    for t in turns:
        if t.role == "user" or not groups:
            groups.append([t])
        else:
            groups[-1].append(t)
    out: List[Round] = []
    for i, g in enumerate(groups):
        text = "\n".join("%s: %s" % (t.role, t.text) for t in g)
        out.append(Round("%s:%d" % (sid, i), sid, i, text, any(t.has_answer for t in g), g[0].date))
    return out


@dataclass
class Question:
    qid: str
    qtype: str
    question: str
    answer: str
    question_date: datetime
    turns: List[Turn]
    gold_session_ids: List[str]
    haystack_session_ids: List[str]
    stale_session_ids: List[str] = field(default_factory=list)  # our own annotation, not in LongMemEval

    @property
    def is_abstention(self) -> bool:
        return self.qid.endswith("_abs")

    @property
    def session_ids(self) -> List[str]:
        return list(self.haystack_session_ids)

    @property
    def rounds(self) -> List[Round]:
        by: Dict[str, List[Turn]] = {}
        for t in self.turns:
            by.setdefault(t.session_id, []).append(t)
        out: List[Round] = []
        for sid in self.haystack_session_ids:
            out.extend(build_rounds(sid, by.get(sid, [])))
        return out

    @property
    def gold_round_ids(self) -> List[str]:
        return [r.round_id for r in self.rounds if r.has_answer]

    @property
    def retrieval_gold_session_ids(self) -> List[str]:
        """Abstention questions are excluded from retrieval metrics even if a gold list is present."""
        return [] if self.is_abstention else list(self.gold_session_ids)

    @property
    def retrieval_gold_round_ids(self) -> List[str]:
        return [] if self.is_abstention else self.gold_round_ids

    @property
    def gold_latest_session_ids(self) -> List[str]:
        """Knowledge-update only (non-abstention): the gold session with the latest date carries the CURRENT
        value; the other gold sessions are stale (superseded). Registered rule; checked against the id
        suffix convention in corpus.verify_dataset. Empty for every other question type."""
        if self.qtype != "knowledge-update" or self.is_abstention or not self.gold_session_ids:
            return []
        dates = session_dates_of(self.turns)
        known = [s for s in self.gold_session_ids if s in dates]
        if not known:
            return []
        return [max(known, key=lambda s: (dates[s], s))]

    @property
    def late_session_ids(self) -> List[str]:
        """Sessions dated after question_date on a timestamp basis (the named 76-question sub-stratum)."""
        seen: Dict[str, datetime] = {t.session_id: t.date for t in self.turns}
        return [s for s, d in seen.items() if d > self.question_date]


def session_dates_of(turns: Sequence[Turn]) -> Dict[str, datetime]:
    out: Dict[str, datetime] = {}
    for t in turns:
        out.setdefault(t.session_id, t.date)
    return out


class DatasetLoader(abc.ABC):
    @abc.abstractmethod
    def questions(self) -> Iterator[Question]:
        ...


class LongMemEvalLoader(DatasetLoader):
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
        seen_sids = set()
        for sid, dstr, sess in zip(sids, dates, sessions):
            if sid in seen_sids:  # a session id repeated inside one haystack (13 refs in S): keep the first
                continue
            seen_sids.add(sid)
            d = parse_date(dstr)
            for i, t in enumerate(sess):
                turns.append(Turn(sid, i, t["role"], t["content"], bool(t.get("has_answer", False)), d))
        return Question(
            qid=str(rec["question_id"]), qtype=rec["question_type"], question=rec["question"],
            answer=str(rec["answer"]), question_date=parse_date(rec["question_date"]), turns=turns,
            gold_session_ids=list(rec.get("answer_session_ids", [])),
            haystack_session_ids=list(dict.fromkeys(sids)),
            stale_session_ids=list(rec.get("x_stale_session_ids", [])),
        )


class FixtureLoader(LongMemEvalLoader):
    def __init__(self, path: Optional[str] = None):
        super().__init__(path or os.path.join(os.path.dirname(__file__), "..", "fixtures", "tiny.json"))


SPLIT_SEED = 16096  # fixed before any data is seen; see PREREGISTRATION.md


def split_pilot_test(qs: Sequence[Tuple[str, str]], seed: int = SPLIT_SEED,
                     pilot_fraction: float = 0.2,
                     clusters: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Deterministic stratified split. qs = [(qid, stratum)] -> {qid: 'pilot'|'test'}.

    Units are clusters (questions sharing a gold session stay together; default: each question alone).
    Within each stratum, units are ordered by sha256("seed:cluster id") and added to the pilot until the
    pilot holds at least ceil(fraction*n) questions (at least one unit when the stratum has >= 2 units,
    never all). Independent of Python version and of input order.
    """
    clusters = clusters or {}
    by: Dict[str, Dict[str, List[str]]] = {}
    for qid, stratum in qs:
        by.setdefault(stratum, {}).setdefault(clusters.get(qid, qid), []).append(qid)
    out: Dict[str, str] = {}
    for stratum, units in by.items():
        n = sum(len(v) for v in units.values())
        want = math.ceil(pilot_fraction * n)
        order = sorted(units, key=lambda c: hashlib.sha256(("%d:%s" % (seed, c)).encode()).hexdigest())
        got = 0
        for i, c in enumerate(order):
            take = len(order) >= 2 and got < want and i < len(order) - 1
            for q in units[c]:
                out[q] = "pilot" if take else "test"
            got += len(units[c]) if take else 0
    return out


DEV2_SIZE = 16


def split_assignment(questions: Sequence[Question]) -> Dict[str, str]:
    """qid -> 'pilot' | 'dev2' | 'test'. Pilot/test as in split_pilot_test (by cluster, stratified by type); then
    `dev2` takes the first DEV2_SIZE knowledge-update question ids, in sorted order, from the test split that
    are not abstention questions and whose cluster holds exactly one question. Nothing else changes."""
    from .corpus import cluster_ids
    clusters = cluster_ids(questions)
    sp = split_pilot_test([(q.qid, q.qtype) for q in questions], clusters=clusters)
    size: Dict[str, int] = {}
    for c in clusters.values():
        size[c] = size.get(c, 0) + 1
    cand = sorted(q.qid for q in questions
                  if sp[q.qid] == "test" and q.qtype == "knowledge-update" and not q.is_abstention
                  and size[clusters[q.qid]] == 1)
    for qid in cand[:DEV2_SIZE]:
        sp[qid] = "dev2"
    return sp


def full_split_assignment(questions: Sequence[Question]) -> Dict[str, str]:
    """qid -> 'pilot' | 'dev2' | 'test' | 'held_back'. Pilot and dev2 are NOT re-derived (split_assignment). Review
    amendment B: every test question whose CONTENT cluster touches a pilot or dev2 question moves to `held_back`
    (never used for a confirmatory result)."""
    from .content import content_clusters, session_hashes
    sp = split_assignment(questions)
    cc = content_clusters(questions, session_hashes(questions))
    touched = {cc[q] for q, v in sp.items() if v in ("pilot", "dev2")}
    return {q: ("held_back" if v == "test" and cc[q] in touched else v) for q, v in sp.items()}


def select_split(questions: Sequence[Question], split: str) -> List[Question]:
    """split in {'all','pilot','test','dev2','held_back'}; computed over ALL questions then filtered."""
    if split == "all":
        return list(questions)
    sp = full_split_assignment(questions)
    return [q for q in questions if sp[q.qid] == split]
