"""Guard for model-facing files and tool outputs: refuse anything that carries a real session id or a label-bearing
id pattern (gold sessions are named `answer_<hash>_<n>`; fillers `sharegpt_`, `ultrachat_` or hex ids)."""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Set

PATTERN = re.compile(r"(?:^|[^A-Za-z0-9])(answer_[0-9a-f]{4,}|sharegpt_[A-Za-z0-9]+|ultrachat_\d+)")
_ROUND = re.compile(r"^(.*):(\d+)$")


class LeakError(ValueError):
    pass


def check(obj: Any, real_sids: Optional[Set[str]] = None, _path: str = "") -> None:
    """Raise LeakError if any string in obj matches a label-bearing pattern or equals a real session id (or a round
    id built from one). Walks dicts and lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            check(k, real_sids, _path + "/key")
            check(v, real_sids, "%s/%s" % (_path, k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            check(v, real_sids, "%s[%d]" % (_path, i))
    elif isinstance(obj, str):
        if PATTERN.search(obj):
            raise LeakError("label-bearing session id pattern at %s" % _path)
        if real_sids:
            m = _ROUND.match(obj)
            if obj in real_sids or (m and m.group(1) in real_sids):
                raise LeakError("real session id at %s" % _path)
