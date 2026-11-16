"""Egress guard at the port: keep real dataset session ids and label-bearing id patterns out of every model-facing string
(prompts, system prompts, stream bodies, session turns, delivered inbox items, injected blocks, tool results). One
implementation for this layer; the pattern follows the eval guard (gold sessions `answer_<hex>_<n>`, fillers `sharegpt_`,
`ultrachat_`) plus a set of exact real ids matched as whole tokens.

Matching runs on NORMALISED text: NFKC (fullwidth letters and underscores fold to ASCII), format characters (zero-width
space, joiners, bidi marks) removed, dash-like punctuation folded to '-', connector punctuation to '_', then casefold. The
hyphen forms (`answer-..`, `sharegpt-..`, `ultrachat-..`) count as ids too; an `answer-` or `sharegpt-` form must contain
a digit so that prose such as "question-answer-feedback" or "sharegpt-style" is not refused.

Real ids: EgressGuard(real_ids=...), or for the process-wide DEFAULT_GUARD the file named by MEMCORE_REAL_IDS_FILE (a JSON
list or one id per line). A guard with require_real_ids=True and no loaded ids refuses EVERY check (fail closed); so does a
configured id file that cannot be read or is empty. MEMCORE_REQUIRE_REAL_IDS=1 makes the id set required.
Unknown value types in a checked structure are refused (fail closed). Error messages never contain the matched text.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional, Set

from .ports import PortError

PATTERN = re.compile(r"(?:^|[^a-z0-9])(answer_[0-9a-f]{4,}|answer-(?=[0-9a-f]*\d)[0-9a-f]{4,}|sharegpt_[a-z0-9]+"
                     r"|sharegpt-(?=[a-z0-9_]*\d)[a-z0-9]+|ultrachat[_-]\d+)")
_SCALARS = (int, float, bool, type(None))


class EgressLeak(PortError, ValueError):
    pass


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append("-" if cat == "Pd" else "_" if cat == "Pc" else ch)
    return "".join(out).casefold()


def _id_key(s: str) -> str:
    return normalise(s).replace("-", "_")


def load_real_ids(path: str) -> Set[str]:
    """A JSON list of ids, or one id per line. Raises (OSError/ValueError) when unreadable or empty: callers fail closed."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    s = raw.strip()
    ids = json.loads(s) if s.startswith("[") else [l.strip() for l in s.splitlines()]
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("real id file must be a JSON list of strings or one id per line")
    ids = {i for i in ids if i}
    if not ids:
        raise ValueError("real id file is empty")
    return ids


class EgressGuard:
    def __init__(self, real_ids: Optional[Iterable[str]] = None, extra_patterns: Iterable[str] = (), require_real_ids: bool = False):
        self.extra = [re.compile(p) for p in extra_patterns]
        self.require_real_ids = require_real_ids
        self.load_error: Optional[str] = None
        self.set_real_ids(real_ids or ())

    def set_real_ids(self, ids: Iterable[str]) -> None:
        """Ids are matched as whole words in normalised text (any characters; '-' and '_' equivalent): an id is found
        unless a letter, digit or '_' touches it on either side, so 'conv-7f.2.' at the end of a sentence still matches."""
        self.real_ids: Set[str] = {_id_key(i) for i in ids if i and _id_key(i).strip()}
        alts = sorted(self.real_ids, key=len, reverse=True)
        self._ids_rx = re.compile(r"(?<![a-z0-9_])(?:%s)(?![a-z0-9_])" % "|".join(map(re.escape, alts))) if alts else None

    def load(self, path: str) -> None:
        """Load the id file and make the id set required. On failure the guard refuses everything (fail closed)."""
        self.require_real_ids = True
        try:
            self.set_real_ids(load_real_ids(path))
            self.load_error = None
        except (OSError, ValueError) as e:
            self.set_real_ids(())
            self.load_error = type(e).__name__

    def check_text(self, s: str, where: str = "text") -> None:
        if self.require_real_ids and not self.real_ids:
            raise EgressLeak("real session id set is required but not loaded (%s); refusing %s (fail closed)" % (self.load_error or "none", where))
        n = normalise(s)
        if PATTERN.search(n) or any(p.search(s) or p.search(n) for p in self.extra):
            raise EgressLeak("label-bearing session id pattern in %s" % where)
        if self._ids_rx is not None and self._ids_rx.search(n.replace("-", "_")):
            raise EgressLeak("real session id in %s" % where)

    def check(self, obj: Any, where: str = "value") -> None:
        if isinstance(obj, str):
            self.check_text(obj, where)
        elif isinstance(obj, (bytes, bytearray, memoryview)):
            self.check_text(bytes(obj).decode("utf-8", "replace"), where)
        elif isinstance(obj, Mapping):
            for k, v in obj.items():
                self.check(k, where + "/key")
                self.check(v, "%s/%s" % (where, k) if isinstance(k, (str, int)) else where + "/value")
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for i, v in enumerate(obj):
                self.check(v, "%s[%d]" % (where, i))
        elif isinstance(obj, _SCALARS):
            if self.require_real_ids and not self.real_ids:
                self.check_text("", where)
        else:
            raise EgressLeak("unsupported value type %s in %s (fail closed)" % (type(obj).__name__, where))


def configure_from_env(guard: EgressGuard, env: Mapping[str, str] = os.environ) -> EgressGuard:
    """Mutates `guard` IN PLACE (DEFAULT_GUARD is captured as a default argument by the adapters, so it must stay one object)."""
    path = env.get("MEMCORE_REAL_IDS_FILE")
    if path:
        guard.load(path)
    elif env.get("MEMCORE_REQUIRE_REAL_IDS") == "1":
        guard.require_real_ids = True
    return guard


DEFAULT_GUARD = configure_from_env(EgressGuard())
