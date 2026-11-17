"""Guard for model-facing files and tool outputs: refuse anything that carries a real session id, a question id, a
level name or a label-bearing id pattern.

- Label patterns (gold sessions are named `answer_<hash>_<n>`; fillers `sharegpt_`, `ultrachat_` or hex ids) and real
  session ids (matched as whole tokens anywhere in a string, so `src:<hexid>:3` and `<hexid>:2:0` are caught) are
  refused in EVERY string, content included.
- In non-content fields (ids, dates, metadata) a question id, a `_abs` token or a level name (H0 to H3) is also refused.
  Content fields (round text, snippets, first words, the question text, reasons) may legitimately mention such strings.
- The top-level key `_meta` is skipped: it carries the question id and level for the runner and must never be passed to
  a model.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Set

PATTERN = re.compile(r"(?:^|[^A-Za-z0-9])(answer_[0-9a-f]{4,}|sharegpt_[A-Za-z0-9]+|ultrachat_\d+)")
TOKEN = re.compile(r"[A-Za-z0-9_]+")
CONTENT_KEYS = {"text", "snippet", "first_words", "question", "reason"}
LEVEL_NAMES = {"H0", "H1", "H2", "H3"}


class LeakError(ValueError):
    pass


def check(obj: Any, real_sids: Optional[Set[str]] = None, qids: Optional[Set[str]] = None,
          _path: str = "", _content: bool = False) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "_meta" and _path == "":
                continue
            check(k, real_sids, qids, _path + "/key", False)
            check(v, real_sids, qids, "%s/%s" % (_path, k), k in CONTENT_KEYS)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            check(v, real_sids, qids, "%s[%d]" % (_path, i), _content)
    elif isinstance(obj, str):
        if PATTERN.search(obj):
            raise LeakError("label-bearing session id pattern at %s" % _path)
        toks = TOKEN.findall(obj)
        if real_sids and any(t in real_sids for t in toks):
            raise LeakError("real session id at %s" % _path)
        if not _content:
            if any(t in LEVEL_NAMES or t.endswith("_abs") for t in toks):
                raise LeakError("level name or _abs question id at %s" % _path)
            if qids and any(t in qids for t in toks):
                raise LeakError("question id at %s" % _path)
