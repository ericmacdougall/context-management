"""Mention matcher m5 (user decision 2026-11-16: erasing a person includes what others said about them; D3 design in
DESIGN-NOTES.md "Design: D3", "Redesign after review C" and "Decisions after review E"; ASSUMPTION under decision 10;
decision R1 decided by the coordinator on the user's delegation, 2026-11-17).

Deterministic, no model: a subject's DECLARED identifiers (names, handles, customer numbers) are reduced to ONE key
token sequence each and stored only as HMAC under the tenant key, with the label label() naming the matcher id. At write
time every event's content is tokenised, every READING of every 1..MAX_N token window is HMAC'd and looked up; a hit
records the subject in the event's `mentions`, and the erasure removes such events WHOLE (decision R1: no span
redaction).

Matcher id (class 3): m5:ucd<unicodedata version>:di<sha12>:cf<sha12>:b<budget>, naming the Unicode version, the two
data tables and the key budget BUDGET. The store keeps the tenant's matcher id and refuses work under another (WPS32 at approval, WPS38 at append and
rescan), so a version change is refused, never silently unmatched.

Tables (class 4): Default_Ignorable_Code_Point of THIS Unicode version and UTS 39 confusables.txt 18.0.0, built by
tools/ucd_tables.py into WP_UCD_DIR (default <WP_HOME>/ucd) and pinned here by sha256. A missing, altered or unpinned
table raises UCDUnavailable: nothing matches without them. They are not in the repository (Unicode License V3 notice).

Per cluster (a base character with its combining marks), default ignorable and format characters removed:
  the NORMAL form   NFKC, casefold, NFKC (an approximation of toNFKC_Casefold): what rule r3 judges and the reviewer reads
  the IDENTITY KEY  canonical(): what each glyph IS; the one key of an identifier. Per code point: a code point whose
                    NFKD is ASCII containing I, 0 or 1 is replaced by that NFKD first; kept as written if its case pair
                    is CLOSED, else case-folded; then the UTS 39 prototype (UTS 39 rev 34 section 4, internalSkeleton
                    steps 3-4: each code point mapped by confusables.txt, NFD), except that the digits 0 and 1 keep
                    themselves; then each prototype mapped to its class representative, repeated until stable.
                    Classes: union-find over prototypes joining the two prototypes of every case pair whose join does
                    not bring together two classes anchored by different ASCII letters or digits (and, outside ASCII,
                    whose prototypes are stable under case folding). Every ASCII letter and digit is its own class;
                    capital I is not closed, so a declared capital I is the letter i.
  the READINGS      both_readings(): in TEXT only, a glyph whose identity and UTS 39 look-alike differ and are both
                    among i, l, o, 0, 1 is read both ways (capital I and its look-alikes as i and l; a digit 1 as 1, l
                    and i; a digit 0 as 0 and o), the digits only in a token whose identity key has more letters than
                    digits. A window's keys are the product of its glyphs' readings, at most MAX_READINGS; a window
                    with more is not enumerated, and an identifier whose CAPACITY (the most readings a matching window
                    can have) exceeds it is refused by rule r3, so no approved identifier is lost (DESIGN-NOTES
                    "Decisions after review E"). not_closed() lists the capitals whose look-alike role is lost
                    (NOT_COVERED).
Tokens are runs of clusters whose normal form is only letters, marks and digits (judged on the normal form, never on the
key); offsets are per cluster in the original text (UAX 15: offsets on normalised text do not map back).

Whole-token-sequence match of keys only (a text window matches when one of its readings is an identifier's key). NOT_COVERED lists what it does not find and OVER_MATCH what it also finds;
erasure receipts state both.

The store binds the scan to the identifier-set version (events.mention_scan_upto vs identifier_epochs.max_seq,
WPS17) and to the matcher (events.mention_matcher, WPS38); events written before a subject's identifiers were declared
are found by the erasure-time rescan (wp/erasure.py).

Budget (m5 b1, DESIGN-NOTES "Matcher budget"): event content is at most MAX_CONTENT_BYTES UTF-8 bytes (ContentTooLarge,
WPM01) and its window keys at most budget(tokens) (MatcherBudgetExceeded, WPM02), both checked before any key is
enumerated or HMAC'd; keys are then enumerated lazily and matched CHUNK at a time, so work is bounded by the budget and
memory by one chunk."""
import collections
import hashlib
import itertools
import os
import pathlib
import unicodedata as UD

UCD = UD.unidata_version
CONFUSABLES_VERSION = "18.0.0"
# sha256 of tools/ucd_tables.py outputs (python3 -I; perl Unicode::UCD of the same version for the ignorables)
PINS = {
    ("default_ignorable", "13.0.0"): "5f861f7bc7299207e9ec92c43b362e98c2e7b48408d76602b27d020a2fe02661",
    ("default_ignorable", "15.0.0"): "24e2227db9c3ca1b3b2233ff6118e5fbc6c9a131b1fb8de1823369b0f1ee54d5",
    ("confusables", "18.0.0"): "93b569e499e542ae2c706ae5613b9a55bb7f3eebeee586300ef77fe978754a6f",
}
MAX_N = 4
MAX_READINGS = 4096          # readings per window (DESIGN-NOTES "Decisions after review E" item 5)
# The key budget, ONE versioned record named in the matcher id (DESIGN-NOTES "Matcher budget (m5 b1)" B1-B3): changing
# any number changes matcher_id(). ASSUMPTION (main's decision, numbers measured by this stream).
Budget = collections.namedtuple("Budget", "per_token floor event_max content_bytes")
BUDGET = Budget(per_token=512, floor=131072, event_max=5_000_000, content_bytes=65536)
MAX_CONTENT_BYTES = BUDGET.content_bytes
CHUNK = 8192                 # distinct window keys HMAC'd and looked up at a time (memory, not the budget)
NOT_COVERED = (
    "identifiers that were never declared (nicknames, misspellings, initials, inflected forms)",
    "pronouns and indirect references",
    "look-alikes outside UTS 39 confusables.txt " + CONFUSABLES_VERSION + " (transliterations, homophones)",
    "a small l written for i or a small i for l (Allson or Aiison for Alison; lan for Ian): distinct letters are "
    "never read as each other; only a capital I and its look-alikes are read both ways",
    "a look-alike of small l or small i from another script written for the OTHER letter (Cyrillic palochka, Latin "
    "dental click, Hebrew vav or Arabic alef for i; Cyrillic small i or dotless i for l): it is read as the letter its "
    "prototype names only (89 look-alikes of l and 39 of i besides the ASCII letters, Unicode 13 and 15)",
    "a letter written for a digit (XQ-l00l or XQ-IOOI for XQ-1001)",
    "digits written for letters in a word with no more letters than digits (A11s0n for Alison)",
    "a digit of another script that looks like 0 or 1: it is read as the letter its prototype names, not as the digit",
    "a capital of another script that looks like one Latin capital while its small form looks like another letter "
    "(Greek capital Nu, Eta; Latin capital I with stroke; the list is not_closed()): it is matched by its small form, "
    "not as the look-alike",
    "look-alikes that are punctuation or symbols (| for l): they end the token",
    "words of the subject quoted by others that contain no declared identifier",
    "names in scripts written without spaces (refused at declaration)",
)
OVER_MATCH = (
    "confusable variants of a declared identifier (for example Kim and Kirn share a key): such events of other "
    "speakers are removed too",
    "a capital I (or one of its look-alikes) in text is read as i and as l, so text in capitals also matches a "
    "name that differs by i and l (DELIA also matches Della, NEIL also matches Nell, HAILEY also matches Halley; "
    "AIEX KIM matches Alex Kim)",
    "a digit 1 or 0 inside a word in text is read as the letters too (Ne1l matches Neil and Nell; J0hn matches John; "
    "a handle such as maria1 matches a declared Marial)",
    "one text window can hit several subjects (EIIa Moreno hits both Elia Moreno and Ella Moreno): erasing either "
    "removes that event",
)


class IdentifierRefused(Exception):
    pass


class UCDUnavailable(Exception):
    """The pinned Unicode tables for this Unicode version are missing or altered: the matcher refuses to run."""


class MatcherMismatch(Exception):
    """The tenant's stored matcher is not this process's matcher: matching would silently miss identifiers."""


class WriteRefused(Exception):
    """A write refused before anything is stored (m5 b1). `code` is stable (WPMnn, distinct from the store's WPSnn);
    `index` is the position of the refused text in the batch; `recorded` is set by the caller that counts it
    (wp/log.py): True, False when counting failed (the refusal stands), None when not counted. Messages carry numbers
    only, never content."""
    code = None

    def __init__(self, msg, index=None):
        super().__init__(f"{self.code} {msg}")
        self.index, self.recorded = index, None


class ContentTooLarge(WriteRefused):
    code = "WPM01"


class MatcherBudgetExceeded(WriteRefused):
    code = "WPM02"


def _table_dir():
    d = os.environ.get("WP_UCD_DIR") or (os.environ.get("WP_HOME") and str(pathlib.Path(os.environ["WP_HOME"]) / "ucd"))
    if not d:
        raise UCDUnavailable("WP_UCD_DIR (or WP_HOME) is not set")
    return d


def load_tables(d, ucd=UCD):
    """(default ignorable code points, {code point: confusable target}) from directory d, each table checked against
    its pin; UCDUnavailable otherwise."""
    d, raw = pathlib.Path(d), {}
    for kind, ver in (("default_ignorable", ucd), ("confusables", CONFUSABLES_VERSION)):
        pin = PINS.get((kind, ver))
        if pin is None:
            raise UCDUnavailable(f"no pinned {kind} table for Unicode {ver}")
        name = f"{kind}-{ver}.tsv"
        try:
            b = (d / name).read_bytes()
        except OSError as e:
            raise UCDUnavailable(f"{name}: {type(e).__name__}") from None
        h = hashlib.sha256(b).hexdigest()
        if h != pin:
            raise UCDUnavailable(f"{name}: sha256 {h[:12]} is not the pinned {pin[:12]}")
        raw[kind] = b.decode("ascii")
    di = frozenset(int(x, 16) for x in raw["default_ignorable"].split())
    conf = {}
    for ln in raw["confusables"].splitlines():
        src, tgt = ln.split("\t")
        conf[int(src, 16)] = "".join(chr(int(x, 16)) for x in tgt.split())
    return di, conf


_TABLES = None


def tables():
    global _TABLES
    if _TABLES is None:
        _TABLES = load_tables(_table_dir())
    return _TABLES


def matcher_id():
    tables()                                   # fail closed before naming a matcher
    return (f"m5:ucd{UCD}:di{PINS[('default_ignorable', UCD)][:12]}"
            f":cf{PINS[('confusables', CONFUSABLES_VERSION)][:12]}:{budget_tag()}")


def budget_tag():
    return "b%d.%d.%d.%d" % BUDGET


def label():
    return f"ident:{matcher_id()}:"            # the ONLY identifier label (tests.test_ident checks no other module has one)


def rule_version():
    return f"r3:{matcher_id()}"


# Rule r3. ASSUMPTION (safe default, not a user decision): a declared identifier links every later event whose text
# contains its key, and erasing the subject removes those events WHOLE, whoever wrote them; so an identifier that is
# likely to name someone else, or that the matcher cannot find reliably, is refused. Multi-token names ("Alex Kim") are
# allowed. Over-erasure by a long but shared name ("Alex Kim" naming two people) is NOT prevented.
MIN_SINGLE = 6
STOP = frozenset("""a an and are as at be by for from he her him his i in is it its me my no not of on or our she so the
their them they this to us we what who with you your mr mrs ms dr sir madam team admin user customer support""".split())
# Scripts written without spaces between words: whole-token matching cannot find a name inside running text.
UNSEGMENTED = frozenset("CJK IDEOGRAPHIC HIRAGANA KATAKANA THAI LAO KHMER MYANMAR TIBETAN".split())
_NOT_SCRIPT = frozenset("MODIFIER SUPERSCRIPT SUBSCRIPT".split())


def _ignorable(c, ident):
    o, cat = ord(c), UD.category(c)
    if o in tables()[0] or cat == "Cf":
        return True
    # unassigned, private-use and surrogate code points: removed from TEXT (they cannot hide a name), but kept in a
    # declared identifier so that rule r3 sees and refuses them (normalisation is stable for assigned characters only)
    return (not ident) and cat in ("Cn", "Co", "Cs")


def _wordch(x, ident):
    cat = UD.category(x)
    return cat[0] in "LNM" or (ident and cat in ("Cn", "Co", "Cs"))


def _fold(s):
    return UD.normalize("NFKC", UD.normalize("NFKC", s).casefold())


_AMB_ASCII = "I01"          # the ASCII glyphs whose UTS 39 prototype is another ASCII letter (I -> l, 1 -> l, 0 -> O)


def _nf(c):
    """A code point whose NFKD is ASCII containing I, 0 or 1 is read as that NFKD (fullwidth, mathematical, superscript
    and circled forms, Roman numerals, the ligature IJ); every other code point as itself (long s stays long s)."""
    d = UD.normalize("NFKD", c)
    return d if d != c and d.isascii() and any(x in d for x in _AMB_ASCII) else c


def _proto(c, conf):
    """UTS 39 prototype of one code point (internalSkeleton step 3); a code point without an entry is its compatibility
    decomposition, each part mapped (fullwidth and mathematical letters)."""
    p = conf.get(ord(c))
    if p is None:
        p = "".join(conf.get(ord(z), z) for z in UD.normalize("NFKD", c))
    return UD.normalize("NFD", p)


def _proto5(c, conf):
    """The prototype the IDENTITY key uses: as _proto, except that the digits 0 and 1 keep themselves (their UTS 39
    prototypes O and l are read only as look-alikes, both_readings())."""
    s = _nf(c)
    if len(s) != 1:
        return "".join(_proto5(x, conf) for x in s)
    return s if s in "01" else _proto(s, conf)


def build_classes(di, conf):
    """(representative map, closed case-changing code points) for these tables. Deterministic: ASCII case pairs first,
    then the others in code point order. A pair is joined (CLOSED) only if the join does not bring together two classes
    anchored by different ASCII letters or digits (the anchor of a letter is the prototype of its SMALL form; m5: ASCII
    pairs too, so capital I, whose prototype is l, is not closed with i) and, outside ASCII, both prototypes are stable
    under case folding (or already closed) (DESIGN-NOTES "Redesign after review E" F11 CORRECTION and "Decisions after
    review E" item 1)."""
    par, anchors = {}, {}

    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for a in "abcdefghijklmnopqrstuvwxyz0123456789":
        p = _proto5(a, conf)
        if len(p) == 1:
            anchors.setdefault(find(p), set()).add(a)
    pairs = []
    for o in range(0x110000):
        c = chr(o)
        if UD.category(c) in ("Cn", "Co", "Cs") or ord(c) in di or len(UD.normalize("NFD", c)) != 1:
            continue
        f = UD.normalize("NFD", c.casefold())
        if f == c or len(f) != 1:
            continue
        pc, pf = _proto5(c, conf), _proto5(f, conf)
        if len(pc) == 1 and len(pf) == 1:
            pairs.append((not (c.isascii() and f.isascii()), o, c, pc, pf))
    closed = set()
    for other, _, c, pc, pf in sorted(pairs):
        if other and not all(x.casefold() == x or x in closed for x in (pc, pf)):
            continue                                        # a prototype that folds (beta -> sharp s -> ss): not closed
        ra, rb = find(pc), find(pf)
        if ra != rb:
            aa, ab = anchors.get(ra, set()), anchors.get(rb, set())
            if aa and ab:
                continue                                    # would bring two ASCII classes together: not closed
            lo, hi = min(ra, rb), max(ra, rb)
            par[hi] = lo
            anchors[lo] = aa | ab
            anchors.pop(hi, None)
        closed.add(c)
    return {x: find(x) for x in par}, frozenset(closed)


_CLASSES = None


def _classes():
    global _CLASSES
    if _CLASSES is None:
        _CLASSES = build_classes(*tables())
    return _CLASSES


def canonical(s):
    """The m5 IDENTITY key of a string (no tokenisation): I/0/1 compatibility forms replaced by their NFKD, case-fold
    unless the case pair is closed, UTS 39 prototype (0 and 1 kept), class representative; repeated until stable (a
    representative may itself be a capital that folds, M -> m -> rn)."""
    di, conf = tables()
    reps, closed = _classes()
    x = UD.normalize("NFD", "".join(_nf(c) for c in UD.normalize("NFD", s) if ord(c) not in di and UD.category(c) != "Cf"))
    for _ in range(6):
        y = []
        for c in x:
            if c not in closed and c.casefold() != c:
                c = UD.normalize("NFD", c.casefold())
            for d in c:
                y.extend(reps.get(e, e) for e in _proto5(d, conf))
        z = UD.normalize("NFD", "".join(y))
        if z == x:
            return x
        x = z
    raise UCDUnavailable("canonical form did not converge: the tables are inconsistent")


_BOTH = None


def _both():
    """{glyph: frozenset of identity keys}: the glyphs read more than one way in TEXT, computed from the tables. A glyph
    whose identity key and look-alike (the identity key of its UTS 39 prototype) differ and are both among i, l, o, 0
    and 1 is read both ways; a digit also as every letter whose capital has the digit's prototype (1 for capital I,
    hence i). DESIGN-NOTES "Decisions after review E" item 2."""
    global _BOTH
    if _BOTH is None:
        di, conf = tables()
        small = frozenset(canonical(x) for x in "ilo01")
        out = {}
        for o in range(0x110000):
            c = chr(o)
            cat = UD.category(c)
            if cat[0] not in "LN" or o in di or len(UD.normalize("NFD", c)) != 1:
                continue
            b = _nf(c)
            if len(b) != 1:
                continue
            ida, la = canonical(c), canonical(_proto(b, conf))
            if ida != la and ida in small and la in small:
                r = {ida, la}
                if cat[0] == "N":
                    p = _proto(b, conf)
                    r.update(canonical(a) for a in "abcdefghijklmnopqrstuvwxyz" if _proto(a.upper(), conf) == p)
                out[c] = frozenset(r)
        _BOTH = out
    return _BOTH


def both_readings():
    """A copy of the both-readings table (tests pin it per Unicode version)."""
    return dict(_both())


_WEIGHTS = None


def _weights():
    """{identity key character: the most readings any glyph that produces it has} (identifier capacity)."""
    global _WEIGHTS
    if _WEIGHTS is None:
        w = {}
        for r in _both().values():
            for ch in r:
                w[ch] = max(w.get(ch, 1), len(r))
        _WEIGHTS = w
    return _WEIGHTS


def _key(cluster):
    """The identity key of one cluster (whitespace from a mapping dropped)."""
    return "".join(c for c in canonical(cluster) if not c.isspace())


_CLUSTERS = {}


def _cluster(cl):
    """(identity key, readings, readings with digits literal) of one cluster. The readings are its keys in TEXT; the
    third form applies in a token with no more letters than digits (DESIGN-NOTES "Decisions after review E" item 3)."""
    r = _CLUSTERS.get(cl)
    if r is not None:
        return r
    ident, both = _key(cl), _both()
    di = tables()[0]
    s = UD.normalize("NFD", "".join(_nf(c) for c in UD.normalize("NFD", cl) if ord(c) not in di and UD.category(c) != "Cf"))
    if not any(c in both for c in s):
        r = (ident, (ident,), (ident,))
    else:
        def alts(lit):
            opts = [sorted(both[c]) if c in both and not (lit and canonical(c) in ("0", "1")) else [c] for c in s]
            return tuple(sorted({"".join(ch for ch in canonical("".join(p)) if not ch.isspace())
                                 for p in itertools.product(*opts)}))
        r = (ident, alts(False), alts(True))
    if len(_CLUSTERS) < 100000:
        _CLUSTERS[cl] = r
    return r


def _tokens(text, ident=False):
    """[(normal token, identity key token, start, end, cluster readings)], start/end code-point offsets in `text`;
    cluster readings: per cluster (readings, readings with digits literal), see _cluster()."""
    out, cur, ck, cr, cs, ce = [], [], [], [], 0, 0
    i, n = 0, len(text)

    def close():
        out.append((UD.normalize("NFKC", "".join(cur)), UD.normalize("NFD", "".join(ck)), cs, ce, tuple(cr)))

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
        cl = "".join(parts)
        f = _fold(cl)
        if f and all(_wordch(x, ident) for x in f):
            if not cur:
                cs = i
            k, full, lit = _cluster(cl)
            cur.append(f)
            ck.append(k)
            cr.append((full, lit))
            ce = j
        elif cur:
            close()
            cur, ck, cr = [], [], []
        i = j
    if cur:
        close()
    return out


def _letters_over_digits(k):
    """True when an identity key has more letters than digits: only then are digit glyphs read as letters too."""
    cats = [UD.category(c)[0] for c in k]
    return cats.count("L") > cats.count("N")


def _token_keys(tok):
    """The text keys of one token, or None when it has more than MAX_READINGS (no approved identifier can match it)."""
    word = _letters_over_digits(tok[1])
    opts = [full if word else lit for full, lit in tok[4]]
    m = 1
    for o in opts:
        m *= len(o)
        if m > MAX_READINGS:
            return None
    if m == 1:
        return (tok[1],)
    return tuple(sorted({UD.normalize("NFD", "".join(p)) for p in itertools.product(*opts)}))


_NOT_CLOSED = None


def not_closed():
    """Case-changing letters whose look-alike role the matcher loses: their UTS 39 look-alike is not among their
    readings, so such a capital is matched by its small form only (NOT_COVERED); computed from the tables, never
    maintained by hand."""
    global _NOT_CLOSED
    if _NOT_CLOSED is None:
        _, conf = tables()
        _, closed = _classes()
        out = []
        for o in range(0x110000):
            c = chr(o)
            if UD.category(c)[0] in "LN" and c not in closed and c.casefold() != c and len(UD.normalize("NFD", c)) == 1:
                look = "".join(ch for ch in canonical(conf.get(o, c)) if not ch.isspace())
                if look not in _cluster(c)[1]:
                    out.append(c)
        _NOT_CLOSED = tuple(out)
    return _NOT_CLOSED


def capacity(identifier):
    """The most readings a text window that matches this identifier can have: the product, over its identity key's
    characters, of _weights() (digits in a token with no more letters than digits count 1). Rule r3 refuses an
    identifier over MAX_READINGS, which is what makes skipping a window over the cap lose nothing."""
    w, m = _weights(), 1
    for tok in _tokens(identifier, ident=True):
        word = _letters_over_digits(tok[1])
        for ch in tok[1]:
            if UD.category(ch)[0] == "N" and not word:
                continue
            m *= w.get(ch, 1)
    return m


def _script(c):
    if UD.category(c)[0] != "L":
        return None
    w = UD.name(c, "").split(" ")[0]
    return None if not w or w in _NOT_SCRIPT else w


_STOP_KEYS = None


def _stop_keys():
    """Every key a common word can have in TEXT, written in lower case, capitalised or in capitals: an identifier made
    only of such keys would match ordinary text ("ln the" has a text key of "In the", "ls lt" of "IS IT")."""
    global _STOP_KEYS
    if _STOP_KEYS is None:
        s = set()
        for w in STOP:
            s.add(key(w))
            for form in (w, w.capitalize(), w.upper()):
                for tok in _tokens(form):
                    s.update(_token_keys(tok) or ())
        _STOP_KEYS = frozenset(s)
    return _STOP_KEYS


def identifier_problem(identifier):
    """None if the identifier (raw or in normal form) is acceptable under rule r3, else the reason. Scripts, unassigned
    code points, digits and length are judged on the NORMAL form (a key hides mixed script); the stop list on KEYS
    ("custorner" has the key of "customer")."""
    toks = _tokens(identifier, ident=True)
    norm = [x[0] for x in toks]
    if not norm:
        return "empty"
    if any(UD.category(c) in ("Cn", "Co", "Cs") for t in norm for c in t):
        return "unassigned or private-use code point (normalisation is not stable for it)"
    if len(norm) > MAX_N:
        return "longer than %d tokens" % MAX_N
    for t in norm:
        scripts = {s for s in map(_script, t) if s}
        if scripts & UNSEGMENTED:
            return "a script written without spaces between words (%s): whole-token matching cannot find it" % ",".join(sorted(scripts))
        if len(scripts) > 1:
            return "a token mixes scripts (%s): look-alike risk" % ",".join(sorted(scripts))
    if all(x[1] in _stop_keys() for x in toks):
        return "only common words (or look-alikes of them)"
    if len(norm) == 1 and len(norm[0]) < MIN_SINGLE and not any(c.isdigit() for c in norm[0]):
        return "single token shorter than %d characters (likely to name someone else; declare the full name)" % MIN_SINGLE
    cap = capacity(identifier)
    if cap > MAX_READINGS:
        return ("up to %d look-alike readings of one window (i, l, o, and 0 or 1 inside a word), over the cap of %d "
                "readings per window: declare a shorter form" % (cap, MAX_READINGS))
    return None


def tokens(text):
    return [x[0] for x in _tokens(text)]


def normalize(identifier):
    """The normal form a reviewer reads and rule r3 judges."""
    return " ".join(x[0] for x in _tokens(identifier, ident=True))


def key(identifier):
    """The ONE key that is HMAC'd for an identifier (per token: the identity key, canonical())."""
    return " ".join(x[1] for x in _tokens(identifier, ident=True))


def check_size(text, index=None, what="event content"):
    """ContentTooLarge (WPM01) when text is over MAX_CONTENT_BYTES UTF-8 bytes. A text with more code points than the
    bound is refused without encoding it (each code point is at least one byte)."""
    if len(text) > MAX_CONTENT_BYTES:
        raise ContentTooLarge(f"{what} {index}: more than {MAX_CONTENT_BYTES} UTF-8 bytes ({len(text)} code points), over the "
                              f"bound of {MAX_CONTENT_BYTES} ({budget_tag()})", index)
    n = len(text.encode("utf-8", "surrogatepass"))
    if n > MAX_CONTENT_BYTES:
        raise ContentTooLarge(f"{what} {index}: {n} UTF-8 bytes, over the bound of {MAX_CONTENT_BYTES} ({budget_tag()})", index)


def budget(n_tokens):
    """The most window keys an event of n_tokens tokens may have (DESIGN-NOTES "Matcher budget" B2)."""
    return min(BUDGET.event_max, max(BUDGET.floor, BUDGET.per_token * n_tokens))


def _reading_count(tok):
    """The number of text keys of one token, or None when it has more than MAX_READINGS (counted, not enumerated)."""
    word = _letters_over_digits(tok[1])
    m = 1
    for full, lit in tok[4]:
        m *= len(full if word else lit)
        if m > MAX_READINGS:
            return None
    return m


def _plan(text):
    """(tokens, reading count per token, window key count): the count is every key of every 1..MAX_N token window,
    repeats included, windows over MAX_READINGS excluded, computed from the counts alone (nothing enumerated)."""
    t = _tokens(text)
    c = [_reading_count(x) for x in t]
    total = 0
    for i in range(len(t)):
        p = 1
        for j in range(i, min(i + MAX_N, len(t))):
            if c[j] is None:
                break
            p *= c[j]
            if p > MAX_READINGS:          # counts are at least 1: every longer window from i is over the cap too
                break
            total += p
    return t, c, total


def key_count(text):
    """(tokens, window keys) of text, counted without enumerating (size not checked)."""
    t, _, total = _plan(text)
    return len(t), total


def _admitted(text, index=None):
    """The plan (tokens, counts) of a text within the size bound and its budget; WPM01 or WPM02 otherwise."""
    check_size(text, index)
    t, c, total = _plan(text)
    b = budget(len(t))
    if total > b:
        raise MatcherBudgetExceeded(f"event {index}: {total} matcher keys for {len(t)} tokens, over the budget of {b} "
                                    f"({budget_tag()}: {BUDGET.per_token} per token, at least {BUDGET.floor}, at most "
                                    f"{BUDGET.event_max})", index)
    return t, c


def admit(texts):
    """Refuse the batch (WriteRefused, naming the first refused text by index) unless every text is within the size
    bound and its key budget. Nothing is enumerated or HMAC'd."""
    for i, x in enumerate(texts):
        _admitted(x, i)


def _iter_windows(t, c):
    """Yield (key, start, end) for every reading of every 1..MAX_N token window of an admitted plan, lazily: only the
    key lists of the current MAX_N tokens are held (each at most MAX_READINGS keys)."""
    cache = {}
    for i in range(len(t)):
        for old in [k for k in cache if k < i]:
            del cache[old]
        p = 1
        for j in range(i, min(i + MAX_N, len(t))):
            if c[j] is None:
                break
            p *= c[j]
            if p > MAX_READINGS:
                break
            for x in range(i, j + 1):
                if x not in cache:
                    cache[x] = _token_keys(t[x])
            a, b = t[i][2], t[j][3]
            for combo in itertools.product(*(cache[x] for x in range(i, j + 1))):
                yield " ".join(combo), a, b


def _windows_at(text):
    """[(key window, start, end)] for every reading of every 1..MAX_N token window, within the budget (tests and
    diagnostics; matching never builds this list). A window with more than MAX_READINGS readings is not enumerated:
    rule r3 refuses every identifier whose capacity exceeds the cap, so no approved identifier can match it."""
    return list(_iter_windows(*_admitted(text)))


def windows(text):
    """The set of window keys of text, within the budget (tests and diagnostics)."""
    return {w for w, _, _ in _iter_windows(*_admitted(text))}


def _stream(ks, tenant, plans, lookup):
    """Match the window keys of each (tag, plan) in `plans` (an iterable, consumed lazily), CHUNK distinct keys at a
    time: HMAC them, call lookup(list of HMACs) -> {hmac: value}, and collect {tag: {(key, value)}} for every hit.
    Holds at most CHUNK pending keys (each with the tags that produced it)."""
    hits, pending = {}, {}

    def flush():
        keys = list(pending)
        hs = _hmacs(ks, tenant, keys)
        found = lookup(hs)
        for k, h in zip(keys, hs):
            if h in found:
                for tag in pending[k]:
                    hits.setdefault(tag, set()).add((k, found[h]))
        pending.clear()

    for tag, plan in plans:
        for w, _, _ in _iter_windows(*plan):
            s = pending.get(w)
            if s is None:
                if len(pending) >= CHUNK:
                    flush()
                pending[w] = {tag}
            else:
                s.add(tag)
    if pending:
        flush()
    return hits


def _hmacs(ks, tenant, keys):
    lab = label()
    return ks.tenant_hmac_many(tenant, [lab + x for x in keys])


window_hmacs = _hmacs        # the ONLY place the identifier label is applied (tests.test_ident checks no other module does)


def check_identifiers(identifiers):
    """The identifiers (stripped, one per key), or IdentifierRefused naming each refused one and why (rule r3)."""
    by_key = {}
    for i in identifiers:
        by_key.setdefault(key(i), i.strip())
    norm = sorted(by_key.values())
    bad = [(n, identifier_problem(n)) for n in norm if identifier_problem(n)] if norm else [("", "no identifiers")]
    if bad:
        raise IdentifierRefused("; ".join(f"{normalize(n)!r}: {why}" for n, why in bad))
    return norm


def propose(conn, ks, tenant, subject_id, identifiers):
    """Propose identifiers for a subject (the application's ONLY way in, D3). Each identifier is sealed AS WRITTEN
    under the subject's key for the reviewer (rule r3 judges its key, which depends on case) and stored with the HMAC
    of its key; nothing is matched until a reviewer approves. The rule runs here too, as early feedback only. Returns
    the proposal ids."""
    import uuid
    from . import keys as K
    ids = check_identifiers(identifiers)
    sk = ks.keys(tenant, [subject_id], create=True)[subject_id]
    rv, out = rule_version(), []
    for n, h in zip(ids, _hmacs(ks, tenant, [key(i) for i in ids])):
        pid = str(uuid.uuid4())
        conn.execute("select wp_identifier_propose(%s::tenant_t, %s, %s, %s, %s, %s)",
                     (tenant, subject_id, pid, h, K.seal(sk, tenant, subject_id, "proposal:" + pid, n), rv))
        out.append(pid)
    return out


def review(rconn, ks, proposal_id):
    """The reviewer process (a wp_reviewer login): opens the sealed identifier, checks it passes rule r3, recomputes the
    HMAC of its key and approves with that HMAC and this matcher's rule version (the store refuses an HMAC other than
    the proposal's, a rule version other than the tenant's matcher, and a reviewer approving its own proposal). A
    failing identifier is rejected with the reason and raised. Returns the new identifier-set version."""
    from . import keys as K
    t, s, enc = rconn.execute("select tenant_id, subject_id, ident_enc from identifier_proposals where proposal_id=%s",
                              (proposal_id,)).fetchone()
    n = K.open_(ks.keys(t, [s])[s], t, s, "proposal:" + proposal_id, bytes(enc))
    why = "cannot open the sealed identifier" if n is None else identifier_problem(n)
    if why:
        rconn.execute("select wp_identifier_reject(%s, %s)", (proposal_id, why))
        raise IdentifierRefused(f"{proposal_id}: {why}")
    h = _hmacs(ks, t, [key(n)])[0]
    return rconn.execute("select wp_identifier_approve(%s, %s, %s)", (proposal_id, h, rule_version())).fetchone()[0]


def init_tenant(tenant):
    """Tenant setup step (an OWNER action, never the app's): store this process's matcher as the tenant's. Refused by
    the store (WPS38) once the tenant has identifiers under another matcher. Experiments and tests call it through
    declare(); a deployment runs it from setup or an admin tool."""
    from . import db
    a = db.admin()
    try:
        a.execute("set role wp_owner")
        a.execute("select wp_matcher_set(%s::tenant_t, %s)", (tenant, matcher_id()))
    finally:
        a.close()


def declare(conn, ks, tenant, subject_id, identifiers, reviewer=None):
    """Tenant setup (init_tenant), propose (through conn) and review (through `reviewer`, a wp_reviewer connection;
    default: reviewer principal 1) in one call, for experiments and tests. Returns the identifier-set version after the
    last approval. Refuses identifiers that fail rule r3, an erased subject (WPS03) and an identifier whose key already
    names another subject (WPS20)."""
    from . import db
    init_tenant(tenant)
    rconn = reviewer or db.reviewer(1)
    try:
        v = None
        for pid in propose(conn, ks, tenant, subject_id, identifiers):
            v = review(rconn, ks, pid)
        return v
    finally:
        if reviewer is None:
            rconn.close()


def scan(conn, ks, tenant, texts):
    """(identifier-set version, [sorted subject ids mentioned, per text]). Every text is admitted first (size bound and
    key budget, m5 b1: WriteRefused naming the text's index), whether or not the tenant has identifiers. The version and
    the tenant's matcher are read BEFORE the identifiers, so a declaration that lands in between makes the store refuse
    the write (WPS17) and the caller rescans. A tenant whose stored matcher is not this one raises MatcherMismatch (the
    store refuses it too: WPS38); a tenant with no matcher has no identifiers. Keys are looked up CHUNK at a time."""
    admit(texts)                 # the whole batch first; plans are rebuilt one text at a time below (none are kept)
    r = conn.execute("select max_seq, matcher from identifier_epochs where tenant_id=%s", (tenant,)).fetchone()
    version, tm = (r[0], r[1]) if r else (0, None)
    if tm is not None and tm != matcher_id():
        raise MatcherMismatch(f"tenant matcher {tm} is not {matcher_id()}")
    if not version:
        return 0, [[] for _ in texts]

    def lookup(hs):
        return dict(conn.execute("select ident_hmac, subject_id from subject_identifiers where tenant_id=%s and ident_hmac = any(%s)",
                                 (tenant, hs)).fetchall())
    hits = _stream(ks, tenant, ((i, _admitted(x, i)) for i, x in enumerate(texts)), lookup)
    return version, [sorted({v for _, v in hits.get(i, ())}) for i in range(len(texts))]


def subject_hmacs(ks, tenant, identifiers):
    return set(_hmacs(ks, tenant, [key(i) for i in identifiers]))


def stream_matches(ks, tenant, items, hmacs):
    """The tags of (tag, text) items whose text contains one of the identifier HMACs, texts admitted one at a time as
    they are consumed (the erasure rescan). A text over the size bound or its budget raises WriteRefused (fail closed)."""
    hm = set(hmacs)
    plans = ((tag, _admitted(text, tag)) for tag, text in items)
    return set(_stream(ks, tenant, plans, lambda hs: {h: True for h in hs if h in hm}))


def matches(ks, tenant, text, hmacs):
    """True when text contains one of the identifier HMACs."""
    return bool(stream_matches(ks, tenant, [(0, text)], hmacs))


def find_spans(ks, tenant, text, hmacs):
    """Ordered, non-overlapping [(start, end)] in `text` covering every window whose HMAC is in hmacs (overlaps merged).
    Diagnostic only (decision R1: an event that matches is removed whole). Within the budget; two lazy passes (the
    matching keys first, CHUNK at a time; then their spans), so nothing proportional to the key count is held."""
    plan = _admitted(text)
    if not hmacs:
        return []
    hm = set(hmacs)
    keys = {k for k, _ in _stream(ks, tenant, [(0, plan)], lambda hs: {h: True for h in hs if h in hm}).get(0, ())}
    hits = sorted((a, b) for w, a, b in _iter_windows(*plan) if w in keys)
    out = []
    for a, b in hits:
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
