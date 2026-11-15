"""Deterministic date-range extraction from a question (no LLM, no external deps). Registered rule.

parse_question_range(text, question_date) -> (start_date, end_date) inclusive, or None.
Priority: absolute dates, then month(+year), then relative expressions. First match wins. Ranges are
widened by a documented slack so a near miss still falls inside; the rule is intentionally simple.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

Range = Tuple[date, date]
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MON_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NUMW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_NUM = r"(\d+|%s)" % "|".join(_NUMW)
_WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUMW[tok]


def _month_range(y: int, m: int) -> Range:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _latest_year(month: int, day: int, qd: date) -> int:
    y = qd.year
    try:
        if date(y, month, day) > qd:
            y -= 1
    except ValueError:
        pass
    return y


def parse_question_range(text: str, question_date: datetime) -> Optional[Range]:
    t = text.lower()
    qd = question_date.date()

    m = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d
        except ValueError:
            pass
    m = re.search(r"\b(%s)\.? (\d{1,2})(?:st|nd|rd|th)?(?:,? (\d{4}))?\b" % _MON_RE, t)
    if m:
        mo, dd = _MONTHS[m.group(1)], int(m.group(2))
        y = int(m.group(3)) if m.group(3) else _latest_year(mo, dd, qd)
        try:
            d = date(y, mo, dd)
            return d, d
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)? (?:of )?(%s)\b(?:,? (\d{4}))?" % _MON_RE, t)
    if m:
        mo, dd = _MONTHS[m.group(2)], int(m.group(1))
        y = int(m.group(3)) if m.group(3) else _latest_year(mo, dd, qd)
        try:
            d = date(y, mo, dd)
            return d, d
        except ValueError:
            pass
    m = re.search(r"\b(%s) (\d{4})\b" % _MON_RE, t)
    if m:
        return _month_range(int(m.group(2)), _MONTHS[m.group(1)])
    m = re.search(r"\b(?:in|during|last|this) (%s)\b" % "|".join(k for k in _MONTHS if len(k) > 3 or k == "may"), t)
    if m and not re.search(r"\b(?:last|this) (?:week|month|year)\b", t[m.start():m.end()]):
        mo = _MONTHS[m.group(1)]
        y = qd.year if mo <= qd.month else qd.year - 1
        return _month_range(y, mo)

    if re.search(r"\byesterday\b", t):
        d = qd - timedelta(days=1)
        return d, d
    if re.search(r"\btoday\b", t):
        return qd, qd
    m = re.search(r"\blast (%s)\b" % "|".join(_WEEKDAYS), t)
    if m:
        wd = _WEEKDAYS[m.group(1)]
        back = (qd.weekday() - wd) % 7 or 7
        d = qd - timedelta(days=back)
        return d, d
    if re.search(r"\blast week\b", t):
        start = qd - timedelta(days=qd.weekday() + 7)
        return start, start + timedelta(days=6)
    if re.search(r"\blast month\b", t):
        y, mo = (qd.year, qd.month - 1) if qd.month > 1 else (qd.year - 1, 12)
        return _month_range(y, mo)
    if re.search(r"\blast year\b", t):
        return date(qd.year - 1, 1, 1), date(qd.year - 1, 12, 31)
    m = re.search(r"\b(?:past|last|previous) %s (day|week|month|year)s?\b" % _NUM, t)
    if m:
        n, unit = _n(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return qd - timedelta(days=days), qd
    m = re.search(r"\b%s (day|week|month|year)s? ago\b" % _NUM, t)
    if m:
        n, unit = _n(m.group(1)), m.group(2)
        centre = qd - timedelta(days={"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n)
        slack = {"day": 1, "week": 3, "month": 15, "year": 60}[unit]
        return centre - timedelta(days=slack), centre + timedelta(days=slack)
    return None


def in_range(session_day: date, rng: Range) -> bool:
    return rng[0] <= session_day <= rng[1]
