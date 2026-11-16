"""Mention matcher m1 (user decision 2026-11-16: erasing a person includes what others said about them).

Deterministic, no model: a subject's DECLARED identifiers (names, handles, customer numbers) are normalised to token
sequences (casefolded runs of letters and digits) and stored only as HMAC under the tenant key. At write time every
event's content is tokenised, every 1..MAX_N token window is HMAC'd and looked up; a hit records the subject in the
event's `mentions`. Exact whole-token-sequence match only: it does NOT find undeclared nicknames, misspellings,
initials, inflected forms ("Xaviers"), pronouns or indirect references. Erasure receipts state exactly this.

The store binds the scan to the identifier-set version (events.mention_scan_upto vs identifier_epochs.max_seq,
WPS17), so an event is never written against an older identifier set than the one committed; events written before a
subject's identifiers were declared are found by the erasure-time rescan (wp/erasure.py)."""
import re

MATCHER = "m1"
MAX_N = 4
_TOK = re.compile(r"[0-9a-z]+")


class IdentifierRefused(Exception):
    pass


# D3 identifier rule. ASSUMPTION (safe default, not a user decision): a declared identifier links every later event
# that contains it, and erasing the subject removes those events whole, whoever wrote them; so an identifier that is
# likely to name someone else is refused. Refused: a single token shorter than MIN_SINGLE characters unless it contains
# a digit (customer numbers such as "XQ1001" are allowed), and any identifier made only of STOP words. Multi-token
# names ("Alex Kim") are allowed. Over-erasure by a long but shared name ("Alex Kim" naming two people) is NOT prevented.
MIN_SINGLE = 6
STOP = frozenset("""a an and are as at be by for from he her him his i in is it its me my no not of on or our she so the
their them they this to us we what who with you your mr mrs ms dr sir madam team admin user customer support""".split())


def identifier_problem(n):
    """None if the normalised identifier is acceptable, else the reason."""
    toks = n.split()
    if not toks:
        return "empty"
    if len(toks) > MAX_N:
        return "longer than %d tokens" % MAX_N
    if all(t in STOP for t in toks):
        return "only common words"
    if len(toks) == 1 and len(toks[0]) < MIN_SINGLE and not any(c.isdigit() for c in toks[0]):
        return "single token shorter than %d characters (likely to name someone else; declare the full name)" % MIN_SINGLE
    return None


def tokens(text):
    return _TOK.findall(text.casefold())


def normalize(identifier):
    return " ".join(tokens(identifier))


def windows(text):
    t = tokens(text)
    return {" ".join(t[i:i + n]) for n in range(1, MAX_N + 1) for i in range(len(t) - n + 1)}


def declare(conn, ks, tenant, subject_id, identifiers):
    """Declare identifiers for a subject. Returns the new identifier-set version. Refuses identifiers that fail the D3
    rule (identifier_problem), an erased subject (WPS03) and an identifier that already names another subject (WPS20).
    The rule runs in the application: the store sees only HMACs and cannot check it."""
    norm = sorted({normalize(i) for i in identifiers})
    bad = [(n, identifier_problem(n)) for n in norm if identifier_problem(n)] if norm else [("", "no identifiers")]
    if bad:
        raise IdentifierRefused("; ".join(f"{n!r}: {why}" for n, why in bad))
    hm = ks.tenant_hmac_many(tenant, ["ident:" + n for n in norm])
    return conn.execute("select wp_identifier_declare(%s::tenant_t, %s, %s)", (tenant, subject_id, hm)).fetchone()[0]


def scan(conn, ks, tenant, texts):
    """(identifier-set version, [sorted subject ids mentioned, per text]). The version is read BEFORE the identifiers,
    so a declaration that lands in between makes the store refuse the write (WPS17) and the caller rescans."""
    r = conn.execute("select max_seq from identifier_epochs where tenant_id=%s", (tenant,)).fetchone()
    version = r[0] if r else 0
    if not version:
        return 0, [[] for _ in texts]
    per = [sorted(windows(x)) for x in texts]
    flat = sorted({w for p in per for w in p})
    if not flat:
        return version, [[] for _ in texts]
    hm = dict(zip(flat, ks.tenant_hmac_many(tenant, ["ident:" + w for w in flat])))
    hit = dict(conn.execute("select ident_hmac, subject_id from subject_identifiers where tenant_id=%s and ident_hmac = any(%s)",
                            (tenant, list(hm.values()))).fetchall())
    return version, [sorted({hit[hm[w]] for w in p if hm[w] in hit}) for p in per]


def subject_hmacs(ks, tenant, identifiers):
    return set(ks.tenant_hmac_many(tenant, ["ident:" + normalize(i) for i in identifiers]))


def matches(ks, tenant, text, hmacs):
    """True when text contains one of the identifier HMACs (used by the erasure rescan)."""
    w = sorted(windows(text))
    return bool(w) and bool(hmacs & set(ks.tenant_hmac_many(tenant, ["ident:" + x for x in w])))
