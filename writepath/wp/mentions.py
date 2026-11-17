"""Mention matcher m4 (user decision 2026-11-16: erasing a person includes what others said about them; D3 design in
DESIGN-NOTES.md "Design: D3" and "Redesign after review C"; ASSUMPTION under decision 10 and decision R1).

Deterministic, no model: a subject's DECLARED identifiers (names, handles, customer numbers) are reduced to KEY token
sequences and stored only as HMAC under the tenant key, with the label label() naming the matcher id. At write time
every event's content is tokenised, every 1..MAX_N key-token window is HMAC'd and looked up; a hit records the subject
in the event's `mentions`, and the erasure removes such events WHOLE (decision R1: no span redaction).

Matcher id (class 3): m4:ucd<unicodedata version>:di<sha12>:cf<sha12>, naming the Unicode version and the two data
tables. The store keeps the tenant's matcher id and refuses work under another (WPS32 at approval, WPS38 at append and
rescan), so a version change is refused, never silently unmatched.

Tables (class 4): Default_Ignorable_Code_Point of THIS Unicode version and UTS 39 confusables.txt 18.0.0, built by
tools/ucd_tables.py into WP_UCD_DIR (default <WP_HOME>/ucd) and pinned here by sha256. A missing, altered or unpinned
table raises UCDUnavailable: nothing matches without them. They are not in the repository (Unicode License V3 notice).

Per cluster (a base character with its combining marks), default ignorable and format characters removed:
  the NORMAL form   NFKC, casefold, NFKC (an approximation of toNFKC_Casefold): what rule r3 judges and the reviewer reads
  the KEY           canonical(): ONE key per token, the same function for identifiers and text (review E F11; DESIGN-
                    NOTES "Redesign after review E"). Per code point: kept as written if its case pair is CLOSED, else
                    case-folded; then the UTS 39 prototype (UTS 39 rev 34 section 4, internalSkeleton steps 3-4: each
                    code point mapped by confusables.txt, NFD); then each prototype mapped to its class representative,
                    repeated until stable. Classes: union-find over prototypes joining the two prototypes of every case
                    pair (ASCII pairs always; any other pair only if its prototypes are stable under case folding and
                    joining does not merge two classes holding different ASCII letters or digits). The standard defines no case-insensitive skeleton; capital I is
                    the look-alike of l and the capital of i, so one key that accepts "AIIson" for "Alison" must make
                    i, l and 1 one class (ASCII classes {i, l, 1}, {o, 0}; OVER_MATCH). not_closed() lists
                    the capitals whose look-alike role is lost (NOT_COVERED).
Tokens are runs of clusters whose normal form is only letters, marks and digits (judged on the normal form, never on the
key); offsets are per cluster in the original text (UAX 15: offsets on normalised text do not map back).

Whole-token-sequence match of keys only. NOT_COVERED lists what it does not find and OVER_MATCH what it also finds;
erasure receipts state both.

The store binds the scan to the identifier-set version (events.mention_scan_upto vs identifier_epochs.max_seq,
WPS17) and to the matcher (events.mention_matcher, WPS38); events written before a subject's identifiers were declared
are found by the erasure-time rescan (wp/erasure.py)."""
import hashlib
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
NOT_COVERED = (
    "identifiers that were never declared (nicknames, misspellings, initials, inflected forms)",
    "pronouns and indirect references",
    "look-alikes outside UTS 39 confusables.txt " + CONFUSABLES_VERSION + " (transliterations, homophones)",
    "a capital of another script that looks like one Latin capital while its small form looks like another letter "
    "(Greek capital Nu, Eta; the list is not_closed()): it is matched by its small form, not as the look-alike",
    "look-alikes that are punctuation or symbols (| for l): they end the token",
    "words of the subject quoted by others that contain no declared identifier",
    "names in scripts written without spaces (refused at declaration)",
)
OVER_MATCH = (
    "confusable variants of a declared identifier (for example Kim and Kirn share a key): such events of other "
    "speakers are removed too",
    "letters merged by look-alike and case together (i, l, 1 and capital I; o and 0; and every look-alike "
    "in confusables.txt): events of other speakers whose words differ from a declared identifier only by such letters "
    "(Allson, Aiison, Ailson or A1ison for Alison; AIEX KIM for Alex Kim) are removed too",
)


class IdentifierRefused(Exception):
    pass


class UCDUnavailable(Exception):
    """The pinned Unicode tables for this Unicode version are missing or altered: the matcher refuses to run."""


class MatcherMismatch(Exception):
    """The tenant's stored matcher is not this process's matcher: matching would silently miss identifiers."""


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
    return (f"m4:ucd{UCD}:di{PINS[('default_ignorable', UCD)][:12]}"
            f":cf{PINS[('confusables', CONFUSABLES_VERSION)][:12]}")


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


def _proto(c, conf):
    """UTS 39 prototype of one code point (internalSkeleton step 3); a code point without an entry is its compatibility
    decomposition, each part mapped (fullwidth and mathematical letters)."""
    p = conf.get(ord(c))
    if p is None:
        p = "".join(conf.get(ord(z), z) for z in UD.normalize("NFKD", c))
    return UD.normalize("NFD", p)


def build_classes(di, conf):
    """(representative map, closed case-changing code points) for these tables. Deterministic: ASCII case pairs first,
    always joined (I/i: capital I is the look-alike of l); then the others in code point order, each joined only if
    both prototypes are stable under case folding (or already closed) and the join does not merge two classes holding
    different ASCII letters or digits (DESIGN-NOTES "Redesign after review E", F11 and its CORRECTION)."""
    par, anchors = {}, {}

    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for a in "abcdefghijklmnopqrstuvwxyz0123456789":
        for v in {a, a.upper()}:
            p = _proto(v, conf)
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
        pc, pf = _proto(c, conf), _proto(f, conf)
        if len(pc) == 1 and len(pf) == 1:
            pairs.append((not (c.isascii() and f.isascii()), o, c, pc, pf))
    closed = set()
    for other, _, c, pc, pf in sorted(pairs):
        if other and not all(x.casefold() == x or x in closed for x in (pc, pf)):
            continue                                        # a prototype that folds (beta -> sharp s -> ss): not closed
        ra, rb = find(pc), find(pf)
        if ra != rb:
            aa, ab = anchors.get(ra, set()), anchors.get(rb, set())
            if other and aa and ab:
                continue                                    # would merge two ASCII classes: not closed
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
    """The m4 key of a string (no tokenisation): case-fold unless the case pair is closed, UTS 39 prototype, class
    representative; repeated until stable (a representative may itself be a capital that folds, M -> m -> rn)."""
    di, conf = tables()
    reps, closed = _classes()
    x = "".join(c for c in UD.normalize("NFD", s) if ord(c) not in di and UD.category(c) != "Cf")
    for _ in range(6):
        y = []
        for c in x:
            if c not in closed and c.casefold() != c:
                c = UD.normalize("NFD", c.casefold())
            for d in c:
                y.extend(reps.get(e, e) for e in _proto(d, conf))
        z = UD.normalize("NFD", "".join(y))
        if z == x:
            break
        x = z
    return x


_NOT_CLOSED = None


def not_closed():
    """Case-changing letters whose look-alike role the key loses: their capital is matched by its small form only
    (NOT_COVERED); computed from the tables, never maintained by hand."""
    global _NOT_CLOSED
    if _NOT_CLOSED is None:
        _, conf = tables()
        _, closed = _classes()
        out = []
        for o in range(0x110000):
            c = chr(o)
            if UD.category(c)[0] in "LN" and c not in closed and c.casefold() != c and len(UD.normalize("NFD", c)) == 1:
                if canonical(c) != canonical(conf.get(o, c)):
                    out.append(c)
        _NOT_CLOSED = tuple(out)
    return _NOT_CLOSED


def _key(cluster):
    """The key of one cluster (whitespace from a mapping dropped)."""
    return "".join(c for c in canonical(cluster) if not c.isspace())


def _tokens(text, ident=False):
    """[(normal token, key token, start, end)], start/end code-point offsets in `text`."""
    out, cur, ck, cs, ce = [], [], [], 0, 0
    i, n = 0, len(text)

    def close():
        out.append((UD.normalize("NFKC", "".join(cur)), UD.normalize("NFD", "".join(ck)), cs, ce))

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
            cur.append(f)
            ck.append(_key(cl))
            ce = j
        elif cur:
            close()
            cur, ck = [], []
        i = j
    if cur:
        close()
    return out


def _script(c):
    if UD.category(c)[0] != "L":
        return None
    w = UD.name(c, "").split(" ")[0]
    return None if not w or w in _NOT_SCRIPT else w


_STOP_KEYS = None


def _stop_keys():
    global _STOP_KEYS
    if _STOP_KEYS is None:
        _STOP_KEYS = frozenset(key(w) for w in STOP)
    return _STOP_KEYS


def identifier_problem(identifier):
    """None if the identifier (raw or in normal form) is acceptable under rule r3, else the reason. Scripts, unassigned
    code points, digits and length are judged on the NORMAL form (a key hides mixed script); the stop list on KEYS
    ("custorner" has the key of "customer")."""
    toks = _tokens(identifier, ident=True)
    norm = [t for t, _, _, _ in toks]
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
    if all(k in _stop_keys() for _, k, _, _ in toks):
        return "only common words (or look-alikes of them)"
    if len(norm) == 1 and len(norm[0]) < MIN_SINGLE and not any(c.isdigit() for c in norm[0]):
        return "single token shorter than %d characters (likely to name someone else; declare the full name)" % MIN_SINGLE
    return None


def tokens(text):
    return [t for t, _, _, _ in _tokens(text)]


def normalize(identifier):
    """The normal form a reviewer reads and rule r3 judges."""
    return " ".join(t for t, _, _, _ in _tokens(identifier, ident=True))


def key(identifier):
    """The key that is HMAC'd (per token: canonical())."""
    return " ".join(k for _, k, _, _ in _tokens(identifier, ident=True))


def _windows_at(text):
    """[(key window, start, end)] for every 1..MAX_N token window (one key per token)."""
    t = _tokens(text)
    return [(" ".join(x[1] for x in t[i:i + n]), t[i][2], t[i + n - 1][3])
            for n in range(1, MAX_N + 1) for i in range(len(t) - n + 1)]


def windows(text):
    return {w for w, _, _ in _windows_at(text)}


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
    """(identifier-set version, [sorted subject ids mentioned, per text]). The version and the tenant's matcher are read
    BEFORE the identifiers, so a declaration that lands in between makes the store refuse the write (WPS17) and the
    caller rescans. A tenant whose stored matcher is not this one raises MatcherMismatch (the store refuses it too:
    WPS38); a tenant with no matcher has no identifiers."""
    r = conn.execute("select max_seq, matcher from identifier_epochs where tenant_id=%s", (tenant,)).fetchone()
    version, tm = (r[0], r[1]) if r else (0, None)
    if tm is not None and tm != matcher_id():
        raise MatcherMismatch(f"tenant matcher {tm} is not {matcher_id()}")
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
    return set(_hmacs(ks, tenant, [key(i) for i in identifiers]))


def matches(ks, tenant, text, hmacs):
    """True when text contains one of the identifier HMACs (used by the erasure rescan)."""
    return bool(find_spans(ks, tenant, text, hmacs))


def find_spans(ks, tenant, text, hmacs):
    """Ordered, non-overlapping [(start, end)] in `text` covering every window whose HMAC is in hmacs (overlaps merged).
    Diagnostic only (decision R1: an event that matches is removed whole)."""
    ws = _windows_at(text)
    if not ws or not hmacs:
        return []
    keys = sorted({w for w, _, _ in ws})
    hm = dict(zip(keys, _hmacs(ks, tenant, keys)))
    hits = sorted((a, b) for w, a, b in ws if hm[w] in hmacs)
    out = []
    for a, b in hits:
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
