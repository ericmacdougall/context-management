"""Canonical identifiers for principals, tenants, groups, model ids and policy keys.

canon(x): NFKC, then casefold, then strip surrounding whitespace. The result must consist ONLY of the characters [a-z0-9._-] (principal,
tenant, group ids) or [a-z0-9._:@/-] (model ids and ban specs). Anything else raises IdError, so:
  - non-ASCII letters (Cyrillic or Greek homoglyphs such as 'mаllory'), combining marks ('mallorý' is 'y' + U+0301), zero-width and
    other format characters, and spaces inside an id are REJECTED, never silently folded into another id;
  - fullwidth forms and case variants fold to the ASCII id under NFKC + casefold (they are the same id, by Unicode's own equivalence).
Rejecting is the fail-closed choice: a directory, policy or session that contains such an id fails to load, and a request naming one denies.

skeleton(x): a PARTIAL ASCII look-alike folding (0->o, 1->l, i->l, 5->s, rn->m, vv->w, separators dropped). It is used only to refuse a
second directory entry that looks like an existing one, and to make revocation over-match (revoking 'mallory' also revokes 'ma1lory').
It is NOT a confusables standard (no UTS #39 table ships with the standard library); look-alikes outside this list are not covered."""
from __future__ import annotations

import re
import unicodedata

PRINCIPAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._:@/-]{0,127}$")
MODEL_SEPARATORS = "-_.:@/"


class IdError(ValueError):
    pass


def _fold(x):
    if not isinstance(x, str):
        raise IdError("id must be str")
    return unicodedata.normalize("NFKC", x).casefold().strip()


def canon(x) -> str:
    """Canonical principal / tenant / group / programme id, or IdError."""
    s = _fold(x)
    if not PRINCIPAL_RE.match(s):
        raise IdError("id is not canonical ASCII [a-z0-9._-]: %r" % (x,))
    return s


def canon_model(x) -> str:
    """Canonical model id, family name or ban spec, or IdError."""
    s = _fold(x)
    if not MODEL_RE.match(s):
        raise IdError("model id is not canonical ASCII [a-z0-9._:@/-]: %r" % (x,))
    return s


_SKEL = [("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("i", "l"), ("5", "s"), ("-", ""), ("_", ""), (".", "")]


def skeleton(x) -> str:
    s = canon(x)
    for a, b in _SKEL:
        s = s.replace(a, b)
    return s
