"""Mention matcher m2 (user decision 2026-11-16: erasing a person includes what others said about them; D3 design in
DESIGN-NOTES.md "Design: D3", ASSUMPTION under decision 10).

Deterministic, no model: a subject's DECLARED identifiers (names, handles, customer numbers) are normalised to token
sequences and stored only as HMAC under the tenant key, with the label LABEL (matcher and Unicode data version). At
write time every event's content is tokenised, every 1..MAX_N token window is HMAC'd and looked up; a hit records the
subject in the event's `mentions`. At erasure the same windows give SPANS (code-point offsets in the original text)
for span redaction (redaction_for).

Normalisation (UAX 15, UTS 39 read 2026-11-17): per cluster (a base character with its combining marks), default
ignorable and format characters removed, then NFKC, casefold, NFKC (an approximation of toNFKC_Casefold). Tokens are
runs of clusters made only of letters, marks and digits; anything else separates tokens. Offsets are kept per cluster,
because normalisation changes length and offsets on normalised text do not map back to the original (UAX 15).

Exact whole-token-sequence match only. NOT_COVERED lists what it does not find; erasure receipts state it.

The store binds the scan to the identifier-set version (events.mention_scan_upto vs identifier_epochs.max_seq,
WPS17), so an event is never written against an older identifier set than the one committed; events written before a
subject's identifiers were declared are found by the erasure-time rescan (wp/erasure.py)."""
import unicodedata as UD

MATCHER = "m2"
UCD = UD.unidata_version
LABEL = f"ident:{MATCHER}:ucd{UCD}:"          # identifiers HMAC'd under one Unicode version never meet text of another
RULE_VERSION = f"r2:{MATCHER}:ucd{UCD}"
MAX_N = 4
MARKER_TOKEN = "erased"
MARKER = "[" + MARKER_TOKEN + "]"              # replaces each redacted span; rule r2 refuses any identifier containing it
NOT_COVERED = (
    "identifiers that were never declared (nicknames, misspellings, initials, inflected forms)",
    "pronouns and indirect references",
    "look-alike characters in other speakers' text (needs the UTS 39 confusable skeleton; confusables.txt not available)",
    "words of the subject quoted by others that contain no declared identifier",
    "names in scripts written without spaces (refused at declaration)",
)


class IdentifierRefused(Exception):
    pass


class NoRedaction(Exception):
    """Span redaction is not possible for this text; the caller falls back to whole removal (tombstone)."""


# Rule r2. ASSUMPTION (safe default, not a user decision): a declared identifier links every later event that contains
# it, and erasing the subject removes or redacts those events, whoever wrote them; so an identifier that is likely to
# name someone else, or that the matcher cannot find reliably, is refused. Multi-token names ("Alex Kim") are allowed.
# Over-erasure by a long but shared name ("Alex Kim" naming two people) is NOT prevented.
MIN_SINGLE = 6
STOP = frozenset("""a an and are as at be by for from he her him his i in is it its me my no not of on or our she so the
their them they this to us we what who with you your mr mrs ms dr sir madam team admin user customer support""".split())
# Scripts written without spaces between words: whole-token matching cannot find a name inside running text.
UNSEGMENTED = frozenset("CJK IDEOGRAPHIC HIRAGANA KATAKANA THAI LAO KHMER MYANMAR TIBETAN".split())
_NOT_SCRIPT = frozenset("MODIFIER SUPERSCRIPT SUBSCRIPT".split())


def _ignorable(c, ident):
    o, cat = ord(c), UD.category(c)
    if cat == "Cf" or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF or 0x180B <= o <= 0x180F \
            or o in (0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0):
        return True
    # unassigned, private-use and surrogate code points: removed from TEXT (they cannot hide a name), but kept in a
    # declared identifier so that rule r2 sees and refuses them (normalisation is stable for assigned characters only)
    return (not ident) and cat in ("Cn", "Co", "Cs")


def _wordch(x, ident):
    cat = UD.category(x)
    return cat[0] in "LNM" or (ident and cat in ("Cn", "Co", "Cs"))


def _fold(s):
    return UD.normalize("NFKC", UD.normalize("NFKC", s).casefold())


def _tokens(text, ident=False):
    """[(normalised token, start, end)], start/end code-point offsets in `text`."""
    out, cur, cs, ce = [], [], 0, 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if _ignorable(c, ident):
            i += 1
            continue
        parts, j = [c], i + 1
        while j < n and (UD.category(text[j])[0] == "M" or _ignorable(text[j], ident)):
            if not _ignorable(text[j], ident):
                parts.append(text[j])
            j += 1
        f = _fold("".join(parts))
        if f and all(_wordch(x, ident) for x in f):
            if not cur:
                cs = i
            cur.append(f)
            ce = j
        elif cur:
            out.append((UD.normalize("NFKC", "".join(cur)), cs, ce))
            cur = []
        i = j
    if cur:
        out.append((UD.normalize("NFKC", "".join(cur)), cs, ce))
    return out


def _script(c):
    if UD.category(c)[0] != "L":
        return None
    w = UD.name(c, "").split(" ")[0]
    return None if not w or w in _NOT_SCRIPT else w


def identifier_problem(n):
    """None if the normalised identifier is acceptable, else the reason."""
    toks = n.split()
    if not toks:
        return "empty"
    if any(UD.category(c) in ("Cn", "Co", "Cs") for c in n):
        return "unassigned or private-use code point (normalisation is not stable for it)"
    if len(toks) > MAX_N:
        return "longer than %d tokens" % MAX_N
    if MARKER_TOKEN in toks:
        return "contains the redaction marker token %r" % MARKER_TOKEN
    for t in toks:
        scripts = {s for s in map(_script, t) if s}
        if scripts & UNSEGMENTED:
            return "a script written without spaces between words (%s): whole-token matching cannot find it" % ",".join(sorted(scripts))
        if len(scripts) > 1:
            return "a token mixes scripts (%s): look-alike risk" % ",".join(sorted(scripts))
    if all(t in STOP for t in toks):
        return "only common words"
    if len(toks) == 1 and len(toks[0]) < MIN_SINGLE and not any(c.isdigit() for c in toks[0]):
        return "single token shorter than %d characters (likely to name someone else; declare the full name)" % MIN_SINGLE
    return None


def tokens(text):
    return [t for t, _, _ in _tokens(text)]


def normalize(identifier):
    return " ".join(t for t, _, _ in _tokens(identifier, ident=True))


def _windows_at(text):
    t = _tokens(text)
    return [(" ".join(x for x, _, _ in t[i:i + n]), t[i][1], t[i + n - 1][2])
            for n in range(1, MAX_N + 1) for i in range(len(t) - n + 1)]


def windows(text):
    return {w for w, _, _ in _windows_at(text)}


def _hmacs(ks, tenant, norms):
    return ks.tenant_hmac_many(tenant, [LABEL + x for x in norms])


window_hmacs = _hmacs        # the ONLY place the identifier label is applied (tests.test_ident checks no other module does)


def check_identifiers(identifiers):
    """Normalised identifiers, or IdentifierRefused naming each refused one and why (rule r2)."""
    norm = sorted({normalize(i) for i in identifiers})
    bad = [(n, identifier_problem(n)) for n in norm if identifier_problem(n)] if norm else [("", "no identifiers")]
    if bad:
        raise IdentifierRefused("; ".join(f"{n!r}: {why}" for n, why in bad))
    return norm


def declare(conn, ks, tenant, subject_id, identifiers):
    """Declare identifiers for a subject. Returns the new identifier-set version. Refuses identifiers that fail the D3
    rule (identifier_problem), an erased subject (WPS03) and an identifier that already names another subject (WPS20).
    The rule runs in the application: the store sees only HMACs and cannot check it."""
    hm = _hmacs(ks, tenant, check_identifiers(identifiers))
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
    hm = dict(zip(flat, _hmacs(ks, tenant, flat)))
    hit = dict(conn.execute("select ident_hmac, subject_id from subject_identifiers where tenant_id=%s and ident_hmac = any(%s)",
                            (tenant, list(hm.values()))).fetchall())
    return version, [sorted({hit[hm[w]] for w in p if hm[w] in hit}) for p in per]


def subject_hmacs(ks, tenant, identifiers):
    return set(_hmacs(ks, tenant, [normalize(i) for i in identifiers]))


def matches(ks, tenant, text, hmacs):
    """True when text contains one of the identifier HMACs (used by the erasure rescan)."""
    return bool(find_spans(ks, tenant, text, hmacs))


def find_spans(ks, tenant, text, hmacs):
    """Ordered, non-overlapping [(start, end)] in `text` covering every window whose HMAC is in hmacs (overlaps merged)."""
    ws = _windows_at(text)
    if not ws or not hmacs:
        return []
    hm = dict(zip(sorted({w for w, _, _ in ws}), _hmacs(ks, tenant, sorted({w for w, _, _ in ws}))))
    hits = sorted((a, b) for w, a, b in ws if hm[w] in hmacs)
    out = []
    for a, b in hits:
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def apply_spans(ks, tenant, text, spans, hmacs):
    """The redacted text. Spans must be exactly the matcher's own spans for (text, hmacs): a redaction is a function of
    the text and the identifiers, never of offsets carried from elsewhere (offset drift cannot produce a partial cut).
    The result is rescanned; any remaining hit refuses the redaction (NoRedaction: the caller removes the event whole)."""
    spans = [tuple(s) for s in spans]
    if not spans:
        raise NoRedaction("no span")
    if any(not (0 <= a < b <= len(text)) for a, b in spans) or any(spans[i][1] > spans[i + 1][0] for i in range(len(spans) - 1)):
        raise NoRedaction("malformed spans")
    if spans != find_spans(ks, tenant, text, hmacs):
        raise NoRedaction("spans differ from the matcher's spans for this text")
    parts, last = [], 0
    for a, b in spans:
        parts += [text[last:a], MARKER]
        last = b
    red = "".join(parts + [text[last:]])
    if red == text or find_spans(ks, tenant, red, hmacs):
        raise NoRedaction("redaction left a match or changed nothing")
    return red


def redaction_for(ks, tenant, text, hmacs):
    """(redacted text, spans) or NoRedaction."""
    sp = find_spans(ks, tenant, text, hmacs)
    return apply_spans(ks, tenant, text, sp, hmacs), sp
