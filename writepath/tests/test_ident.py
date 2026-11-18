"""Offline unit test (no stores) for D3 rule r3 and matcher m5 (wp/mentions.py), DESIGN-NOTES "Redesign after review C"
classes 3 and 4, "Redesign after review E" F11 and "Decisions after review E" (scenario ids S1..S16 refer to "Design:
D3"). ASSUMPTION: decision 10 and rule r3. Decision R1 (no span redaction; whole-event removal only) was decided by the
coordinator on the user's delegation, 2026-11-17.

The matcher's Unicode tables are NOT in the repository (licence: DESIGN-NOTES class 4). WP_UCD_DIR must name the
directory that tools/ucd_tables.py wrote for this Python's Unicode version; without it every test here FAILS (never
skips): a matcher without its tables must not look tested. A fake key service HMACs with a fixed test key, so matching
works exactly as with the real tenant key.
usage (from writepath/): WP_UCD_DIR=<dir> python3 -m unittest tests.test_ident -v"""
import hashlib
import hmac
import importlib
import os
import pathlib
import random
import re
import shutil
import sys
import tempfile
import unicodedata
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
M = importlib.import_module("wp.mentions")
UCD = unicodedata.unidata_version
UCD_DIR = os.environ.get("WP_UCD_DIR", "")


def setUpModule():
    if not UCD_DIR or not (pathlib.Path(UCD_DIR) / f"default_ignorable-{UCD}.tsv").is_file():
        raise RuntimeError(f"WP_UCD_DIR={UCD_DIR!r} has no default_ignorable-{UCD}.tsv: build it with tools/ucd_tables.py")


class FakeKS:
    KEY = b"test-only-fixed-key"

    def tenant_hmac_many(self, tenant, texts):
        return [hmac.new(self.KEY, f"{tenant}|{x}".encode(), hashlib.sha256).hexdigest() for x in texts]


KS, T = FakeKS(), "t1"


def hm(*idents):
    return M.subject_hmacs(KS, T, idents)


def found(text, *idents):
    """The substrings of the ORIGINAL text that the matcher reports for these identifiers."""
    return [text[a:b] for a, b in M.find_spans(KS, T, text, hm(*idents))]


class RuleR3(unittest.TestCase):
    def ok(self, ident):
        self.assertIsNone(M.identifier_problem(M.normalize(ident)), ident)

    def bad(self, ident, why):
        p = M.identifier_problem(M.normalize(ident))
        self.assertIsNotNone(p, ident)
        self.assertIn(why, p, ident)

    def test_s1_short_or_common_refused(self):
        self.bad("alex", "shorter")
        self.bad("the team", "common")
        self.bad("ADMIN", "common")
        self.ok("Alex Kim")
        self.ok("XQ-1001")

    def test_s1_lookalike_of_a_common_word_refused(self):
        # its skeleton is the skeleton of "customer": it would match every "customer" in other speakers' text
        self.bad("custorner", "common")
        self.bad("adrnin", "common")
        # m5: a capital I in TEXT is read as l too, so "In the" and "IS IT" have the text keys of "ln the" and "ls lt";
        # an identifier with those keys would match ordinary text (advisor check on abceae9)
        self.bad("ln the", "common")
        self.bad("ls lt", "common")
        self.bad("lT lS", "common")

    def test_s5_mixed_script_declared_identifier_refused(self):
        self.bad("Аlexander Kim", "script")          # Cyrillic capital A inside a Latin name
        self.bad("Aleхander", "script")              # Cyrillic small ha
        self.ok("Александр Ким")                     # all Cyrillic

    def test_s16_unsegmented_script_refused(self):
        self.bad("山田太郎", "spaces")                # Han
        self.bad("สมชาย", "spaces")                  # Thai

    def test_unassigned_code_point_refused(self):
        self.bad("Alex\u0378ander Kim", "unassigned")

    def test_s3_normal_form_keeps_letters(self):
        self.assertEqual(M.normalize("José García"), "josé garcía")
        self.assertEqual(M.normalize("JOSÉ GARCÍA"), "josé garcía")
        self.assertEqual(M.normalize("Marta STRASSE"), M.normalize("marta straße"))
        self.assertEqual(M.normalize("Ａｌｅｘ Kim"), "alex kim")

    def test_s4_ignorables_removed_from_declared_identifier(self):
        self.assertEqual(M.normalize("Al\u200dex K\u200bim"), "alex kim")
        self.assertEqual(M.normalize("Alex\ufe0f Kim"), "alex kim")
        self.assertEqual(M.normalize("Al\u17b4ex Kim"), "alex kim")


class Tables(unittest.TestCase):
    """Class 4: the tables are Unicode data pinned by sha256; anything else fails closed."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_tables(self):
        for p in pathlib.Path(UCD_DIR).glob("*.tsv"):
            shutil.copy(p, self.tmp / p.name)

    def test_loads_the_pinned_tables(self):
        di, conf = M.load_tables(UCD_DIR)
        self.assertIn(0x17B4, di)
        self.assertIn(0x200B, di)
        self.assertEqual(conf[ord("m")], "rn")

    def test_missing_table_fails_closed(self):
        with self.assertRaises(M.UCDUnavailable):
            M.load_tables(self.tmp)
        with self.assertRaises(M.UCDUnavailable):
            M.load_tables(self.tmp / "absent")

    def test_tampered_table_fails_closed(self):
        self.copy_tables()
        p = self.tmp / f"default_ignorable-{UCD}.tsv"
        p.write_text(p.read_text().replace("17B4\n", ""))
        with self.assertRaises(M.UCDUnavailable):
            M.load_tables(self.tmp)

    def test_other_unicode_version_fails_closed(self):
        self.copy_tables()
        with self.assertRaises(M.UCDUnavailable):
            M.load_tables(self.tmp, ucd="99.0.0")

    def test_matcher_id_names_unicode_version_and_tables(self):
        mid = M.matcher_id()
        self.assertRegex(mid, rf"^m5:ucd{re.escape(UCD)}:di[0-9a-f]{{12}}:cf[0-9a-f]{{12}}:b\d+(\.\d+){{7}}$")   # b: the budget (m5 b1; review H adds the token cap)
        di = hashlib.sha256((pathlib.Path(UCD_DIR) / f"default_ignorable-{UCD}.tsv").read_bytes()).hexdigest()[:12]
        self.assertIn(f":di{di}:", mid)
        self.assertEqual(M.label(), f"ident:{mid}:")
        self.assertEqual(M.rule_version(), f"r3:{mid}")

    def test_hmac_label_binds_the_matcher(self):
        self.assertEqual(M.subject_hmacs(KS, T, ["Alex Kim"]), set(KS.tenant_hmac_many(T, [M.label() + M.key("Alex Kim")])))


class LabelInOnePlace(unittest.TestCase):
    def test_s15_no_module_builds_identifier_hmacs_itself(self):
        """A second copy of the label (erasure's rescan had "ident:" + w) silently stops matching when the label
        changes: every identifier HMAC must come from wp/mentions.py."""
        offenders = [p.name for p in (HERE / "wp").glob("*.py")
                     if p.name != "mentions.py" and ('"ident:' in p.read_text() or "'ident:" in p.read_text())]
        self.assertEqual(offenders, [])


class WholeDefaultIgnorable(unittest.TestCase):
    """Class 4 (review C F6): EVERY Default_Ignorable_Code_Point of this Unicode version, inside a name in other
    speakers' text, still matches, and the reported span is the original text including it."""

    def test_every_default_ignorable_inside_a_name(self):
        table = (pathlib.Path(UCD_DIR) / f"default_ignorable-{UCD}.tsv").read_text().split()
        self.assertGreater(len(table), 4000, "the whole property, not a sample")
        missed = []
        for h in table:
            name = f"Al{chr(int(h, 16))}ex Kim"
            if found(f"call {name} now", "Alex Kim") != [name]:
                missed.append(h)
        self.assertEqual(missed, [], f"{len(missed)} of {len(table)} default ignorables hide the name")

    def test_f6_khmer_inherent_vowels(self):
        for c in ("\u17b4", "\u17b5"):
            t = f"call Al{c}ex Kim now"
            self.assertEqual(found(t, "Alex Kim"), [f"Al{c}ex Kim"], hex(ord(c)))


class Matcher(unittest.TestCase):
    def test_s2_whole_tokens_only(self):
        self.assertEqual(found("Alex Kimball called", "Alex Kim"), [])
        self.assertEqual(found("Alex Kim's car", "Alex Kim"), ["Alex Kim"])

    def test_s3_offsets_are_in_the_original_text(self):
        for t, ident, want in (("call ALEX KIM now", "Alex Kim", "ALEX KIM"),
                               ("Ａｌｅｘ Ｋｉｍ!", "Alex Kim", "Ａｌｅｘ Ｋｉｍ"),
                               ("ask JOSÉ GARCÍA.", "José García", "JOSÉ GARCÍA"),
                               ("Marta STRASSE wrote", "Marta Straße", "Marta STRASSE")):
            self.assertEqual(found(t, ident), [want], (t, ident))

    def test_s4_invisible_characters_inside_a_name_in_text(self):
        self.assertEqual(found("met Al\u200dex K\u200bim today", "Alex Kim"), ["Al\u200dex K\u200bim"])

    def test_s6_lookalikes_in_other_speakers_text_match(self):
        for name in ("AIex Kim",          # capital I for l
                     "A1ex Kim",          # digit one for l
                     "Аlex Kim",          # Cyrillic capital A
                     "Alex Kirn",         # rn for m
                     "Alex Кim"):         # Cyrillic capital Ka
            self.assertEqual(found(f"please call {name} today", "Alex Kim"), [name], name)

    def test_s6_one_case_insensitive_key_per_identifier(self):
        self.assertEqual(M.key("ALEX KIM"), M.key("Alex Kim"))
        self.assertEqual(hm("Alex Kim"), hm("ａｌｅｘ kirn"))
        # m5: a digit in a DECLARED identifier is a digit (one key, its identity); in TEXT it is read as l too
        self.assertNotEqual(M.key("A1ex Kirn"), M.key("Alex Kim"))
        self.assertEqual(found("call A1ex Kirn now", "Alex Kim"), ["A1ex Kirn"])

    def test_s6_capital_i_lookalike_in_text(self):
        # m5 (main's ruling after review E): in TEXT a capital I is read as i and as l; a declared capital I is i
        self.assertNotEqual(M.key("AIex Kim"), M.key("Alex Kim"))
        self.assertEqual(found("AIex Kim", "Alex Kim"), ["AIex Kim"])
        self.assertEqual(found("AIEX KIM", "Alex Kim"), ["AIEX KIM"])          # stated over-match (OVER_MATCH)
        self.assertEqual(found("Bill Kim", "Alex Kim"), [])

    def test_s7_identifier_inside_quoted_text_of_another_speaker(self):
        self.assertEqual(found('Zoe wrote: "please call Alex Kim tomorrow"', "Alex Kim"), ["Alex Kim"])

    def test_two_occurrences_and_overlap_merged(self):
        t = "Alex Kim and alex kim; XQ-1001"
        got = M.find_spans(KS, T, t, hm("Alex Kim", "XQ-1001", "Kim XQ"))
        self.assertEqual([t[a:b] for a, b in got], ["Alex Kim", "alex kim; XQ-1001"])
        self.assertTrue(all(got[i][1] <= got[i + 1][0] for i in range(len(got) - 1)), "ordered, non-overlapping")

    def test_r1_no_span_redaction_api(self):
        """Decision R1 (review C F1): whole-event removal is the only path; the matcher offers no redaction."""
        for name in ("redaction_for", "apply_spans", "NoRedaction", "MARKER"):
            self.assertFalse(hasattr(M, name), name)


CAP = 4096       # m5: readings per window (DESIGN-NOTES "Decisions after review E" item 5)
# The glyphs read more than one way, by reading set (ASCII names), and the not-closed list, computed by the matcher from
# the pinned tables; pinned here per Unicode version (a version not listed fails: pin it after checking the list).
BOTH = {"13.0.0": {"il": 23, "1il": 11, "0o": 11}, "15.0.0": {"il": 23, "1il": 11, "0o": 11}}
NOT_CLOSED = {"13.0.0": 66, "15.0.0": 66}


class F11M5Readings(unittest.TestCase):
    """Main's ruling after review E (matcher m5): i, l and 1 are never one letter. Only ambiguous glyphs get more than
    one reading, and only in TEXT: a capital I (and its look-alikes) is read as i and as l, a digit 1 inside a word as
    1, l and i, a digit 0 inside a word as 0 and o. A declared identifier has ONE key. The generator takes its
    substitutions from the RAW confusables table, restricted to what the design reads; the documented exceptions are
    tested as misses."""
    IDENTS = ("Alison", "Alex Kim", "Zoë Renée", "Marta Straße", "Nguyệt Lê", "Александр Ким", "Ηλίας Παππάς",
              "Kimberly Moore", "XQ-1001", "Delia Smith", "Neil Young")
    PAIRS = (("Delia Smith", "Della Smith"), ("Hailey Brown", "Halley Brown"), ("Neil Young", "Nell Young"),
             ("Elia Moreno", "Ella Moreno"))
    SEED, PER_IDENT = 16096, 300

    @classmethod
    def setUpClass(cls):
        di, conf = M.load_tables(UCD_DIR)
        cls.di = sorted(di)
        cls.conf = {chr(k): v for k, v in conf.items()}
        cls.by_proto, cls.by_nfkd = {}, {}
        for o in range(0x110000):
            s = chr(o)
            if unicodedata.category(s)[0] not in "LN":
                continue
            d = unicodedata.normalize("NFKD", s)
            if s in cls.conf and len(d) == 1:
                cls.by_proto.setdefault(cls.conf[s], []).append(s)
            if d != s and len(d) == 1 and d in "0123456789IOl":
                cls.by_nfkd.setdefault(d, []).append(s)
        cls.small_proto = {cls.conf.get(a, a): a for a in "abcdefghijklmnopqrstuvwxyz"}
        # the design's both-readings glyphs, from the raw data: compatibility forms of I, 1 and 0, and the capitals
        # the design names (Greek Iota, Cyrillic Byelorussian-Ukrainian I, Latin Iota)
        i_like = set(cls.by_nfkd.get("I", ())) | {"I", "Ι", "І", "Ɩ"}
        one_like = set(cls.by_nfkd.get("1", ())) | {"1"}
        zero_like = set(cls.by_nfkd.get("0", ())) | {"0"}
        cls.reads_as = {"i": i_like | one_like, "l": i_like | one_like, "o": zero_like}
        cls.nc = set(M.not_closed())

    def matches(self, text, ident):
        return len(found(f"please ask {text} today", ident)) == 1

    # -- main's ruling: distinct letters never come together -------------------------------------------------------
    def test_m5_real_name_pairs_stay_apart(self):
        bad = []
        for a, b in self.PAIRS:
            if M.key(a) == M.key(b):
                bad.append(("same key", a, b))
            for x, y in ((a, b), (b, a)):
                for form in (x, x.lower()):
                    if self.matches(form, y):
                        bad.append((form, "matched", y))
        self.assertEqual(bad, [])

    def test_m5_ascii_letters_and_digits_are_their_own_keys(self):
        keys = {}
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
            keys.setdefault(M.key(ch), []).append(ch)
            if ch.isalpha():
                self.assertEqual(M.key(ch), M.key(ch.upper()), ch)       # a DECLARED capital is its letter: I is i
        self.assertEqual(sorted("".join(v) for v in keys.values() if len(v) > 1), [])

    def test_m5_identifiers_with_digits_stay_distinct(self):
        forms = ("XQ-1001", "XQ-l00l", "XQ-IOOI", "XQ-lool", "user1001", "userlool")
        self.assertEqual(len({M.key(x) for x in forms}), len(forms))
        self.assertEqual(found("ref XQ-1001", "XQ-1001"), ["XQ-1001"])
        for text, ident in (("ref XQ-1001", "XQ-l00l"), ("ref XQ-1001", "XQ-lool"), ("ref XQ-l00l", "XQ-1001"),
                            ("ref XQ-IOOI", "XQ-1001"), ("login user1001", "userlool")):
            self.assertEqual(found(text, ident), [], (text, ident))

    # -- what still matches -------------------------------------------------------------------------------------------
    def test_m5_reviewer_counterexample_still_matches(self):
        # review E F11: one capital I is the look-alike of l, the other the capital of i
        for v in ("AIIson", "AlIson", "aLIsOn", "ALISON", "A1ison", "AIis0n"):
            self.assertTrue(self.matches(v, "Alison"), v)

    def test_m5_digit_inside_a_word_is_read_as_a_letter(self):
        for text, ident in (("A1ison", "Alison"), ("J0hn Smith", "John Smith"), ("N1ck Jones", "Nick Jones"),
                            ("Kimber1y M0ore", "Kimberly Moore"), ("XQ-１００１", "XQ-1001")):
            self.assertTrue(self.matches(text, ident), (text, ident))

    def test_m5_reordered_combining_marks(self):
        # e + dot below (ccc 220) + circumflex (230) in either order is canonically equivalent (UAX 15)
        self.assertTrue(self.matches("Nguyệt Lê", "Nguyệt Lê"))
        self.assertTrue(self.matches("NGUYỆT LÊ", "Nguyệt Lê"))

    def subs(self, c):
        """Glyphs the design reads as c, from the raw table (not the matcher)."""
        if c.isdigit():
            return sorted(self.by_nfkd.get(c, ()))          # compatibility forms only: a letter is never read as a digit
        p = self.conf.get
        lo, up = c.lower(), c.upper()
        if {c, up} & self.nc:
            # a capital whose case pair is not closed is matched by its small form (published exception, NOT_COVERED)
            out = set(self.by_proto.get(p(c.casefold(), c.casefold()), ()))
        else:
            out = set(self.by_proto.get(p(lo, lo), ()))
            if len(up) == 1:
                pu = p(up, up)
                if self.small_proto.get(pu, lo) == lo:      # a capital that looks like ANOTHER small letter (I, l): no
                    out |= set(self.by_proto.get(pu, ()))
        out |= self.reads_as.get(lo, set()) if c.isascii() else set()
        if lo == "i":
            out -= set(self.by_proto.get("l", ())) - self.reads_as["i"]   # small-l look-alikes are not read as i
        return sorted(out - self.nc)

    def variant(self, rnd, ident):
        src = unicodedata.normalize("NFC", ident)
        tok = []                                            # token number of each character (separators: -1)
        t = 0
        for c in src:
            if c.isalnum():
                tok.append(t)
            else:
                tok.append(-1)
                t += 1
        letters = {}
        for c, k in zip(src, tok):
            if c.isalpha():
                letters[k] = letters.get(k, 0) + 1
        digits = {k: sum(1 for c, j in zip(src, tok) if j == k and c.isdigit()) for k in letters}
        out = []
        for c, k in zip(src, tok):
            r = rnd.random()
            if r < 0.25 and c.isalpha():
                c = c.swapcase() if len(c.swapcase()) == 1 else c
            elif r < 0.55 and c.isalnum():
                choices = self.subs(c)
                if c.isalpha():
                    # a digit inside a word is read as a letter only while the word has more letters than digits
                    if not letters[k] - 1 > digits[k] + 1:
                        choices = [s for s in choices if not unicodedata.normalize("NFKD", s).isdigit()]
                if choices:
                    s = rnd.choice(choices)
                    if c.isalpha() and unicodedata.normalize("NFKD", s).isdigit():
                        digits[k] += 1
                        letters[k] -= 1
                    c = s
            out.append(c)
            if rnd.random() < 0.15:
                out.append(chr(rnd.choice(self.di)))
        v = "".join(out)
        return unicodedata.normalize(rnd.choice(("NFC", "NFD")), v)

    def test_m5_generated_variants_match(self):
        rnd, missed, n = random.Random(self.SEED), [], 0
        for ident in self.IDENTS:
            for _ in range(self.PER_IDENT):
                v = self.variant(rnd, ident)
                n += 1
                if not self.matches(v, ident):
                    missed.append((ident, v, [hex(ord(c)) for c in v]))
        self.assertEqual(missed[:5], [], f"{len(missed)} of {n} generated variants not matched")

    # -- what does not match --------------------------------------------------------------------------------------
    def test_m5_near_misses_do_not_match(self):
        letters = "abcdefghijklmnopqrstuvwxyz"
        bad = []
        for ident in ("Alison", "Alex Kim", "Kimberly Moore", "Delia Smith", "Neil Young"):
            for i, c in enumerate(ident):
                if not c.isalpha():
                    continue
                for x in letters:
                    if x == c.lower():
                        continue
                    for v in (ident[:i] + x + ident[i + 1:], ident[:i] + x.upper() + ident[i + 1:]):
                        if v[i] == "I" and c.lower() in "il":
                            continue                        # a capital I is read as i and as l (the design)
                        if self.matches(v, ident):
                            bad.append(v)
                drop = ident[:i] + ident[i + 1:]
                if self.matches(drop, ident):
                    bad.append(drop)
                for x in letters:
                    ins = ident[:i] + x + ident[i:]
                    if self.matches(ins, ident):
                        bad.append(ins)
        self.assertEqual(bad[:10], [], f"{len(bad)} near misses matched")
        self.assertFalse(self.matches("Allison", "Alison"))
        self.assertFalse(self.matches("Alisov", "Alison"))

    def test_m5_documented_misses(self):
        """NOT_COVERED (m5): small l written for i and small i for l, letters for digits, digits in a token with no
        more letters than digits."""
        for text, ident in (("Allson", "Alison"), ("Aiison", "Alison"), ("lan Smith", "Ian Smith"),
                            ("XQ-l00l", "XQ-1001"), ("XQ-IOOI", "XQ-1001"), ("A11s0n", "Alison"),
                            ("AlӀson", "Alison"),        # Cyrillic palochka (looks like l) for i
                            ("Aіex Kim", "Alex Kim")):    # Cyrillic small i (looks like i) for l
            self.assertFalse(self.matches(text, ident), (text, ident))
        nc = " ".join(M.NOT_COVERED)
        for word in ("Allson", "XQ-l00l", "A11s0n", "palochka"):
            self.assertIn(word, nc)

    def test_m5_stated_over_match(self):
        for text, ident in (("AIEX KIM", "Alex Kim"), ("DELIA SMITH", "Della Smith"), ("NEIL YOUNG", "Nell Young"),
                            ("HAILEY BROWN", "Halley Brown"), ("Ne1l Young", "Neil Young"), ("Ne1l Young", "Nell Young"),
                            ("EIIa Moreno", "Elia Moreno"), ("EIIa Moreno", "Ella Moreno"),
                            ("maria1 Smith", "Marial Smith")):  # a word ending in a digit: the digit read as l
            self.assertTrue(self.matches(text, ident), (text, ident))
        om = " ".join(M.OVER_MATCH)
        for word in ("DELIA", "Ne1l", "several subjects", "maria1"):
            self.assertIn(word, om)
        self.assertNotIn("Delia and Della", om)     # plain text naming one of them no longer matches the other

    # -- the cap ------------------------------------------------------------------------------------------------
    def test_m5_cap_refuses_identifiers_over_it(self):
        self.assertIsNone(M.identifier_problem("Boooo Boooo Boooo"))         # 12 o: at most 2**12 = 4096 readings
        for over in ("Boooo Boooo Booooo", "Lillian Oliveira"):              # 8192; 3**8 * 2 = 13122
            p = M.identifier_problem(over)
            self.assertIsNotNone(p, over)
            self.assertIn(str(CAP), p, over)
            with self.assertRaises(M.IdentifierRefused):
                M.check_identifiers([over])
        for ok in ("Alison Smith", "Kimberly Moore", "XQ-1001", "100010001", "A1B0C1D0E1"):
            self.assertIsNone(M.identifier_problem(ok), ok)

    def test_m5_window_over_the_cap_is_not_enumerated(self):
        self.assertEqual(len(M.windows("I" * 12)), CAP)                      # 2**12 readings: every one
        self.assertEqual(M.windows("I" * 13 + " Kim"), {M.key("Kim")})      # 2**13: no identifier can match it
        self.assertLessEqual(len(M.windows("I1I1I1 Kim")), CAP + 1)

    # -- invariants --------------------------------------------------------------------------------------------
    def test_m5_identity_key_is_one_of_the_text_keys(self):
        rnd = random.Random(self.SEED + 1)
        plain = [x for x in self.IDENTS if not any(c in x for c in "I10")]
        samples = list(self.IDENTS) + ["AIIson", "A1ison", "J0hn Smith", "ΗΛΊΑΣ", "Ⅱ Kim", "ＡＩＩＳＯＮ"]
        samples += [self.variant(rnd, i) for i in self.IDENTS for _ in range(20)]
        bad = []
        for s in samples:
            ws = M._windows_at(s)
            lo, hi = min(a for _, a, _ in ws), max(b for _, _, b in ws)
            whole = {w for w, a, b in ws if (a, b) == (lo, hi)}
            if M.key(s) not in whole:
                bad.append(("identity key not a text key", s))
            if s in plain and whole != {M.key(s)}:
                bad.append(("more than one key without a both-readings glyph", s, len(whole)))
        self.assertEqual(bad[:5], [], f"{len(bad)} broken")
        for x in ("0", "1", "I", "l", "o", "O", "i"):
            k = M.canonical(x)
            self.assertEqual(M.canonical(k), k, x)
        self.assertEqual((M.canonical("1"), M.canonical("0")), ("1", "0"))

    def test_m5_both_readings_glyphs(self):
        """Pinned per Unicode version; each reading checked through the text path, and no other reading."""
        name = {M.canonical(x): x for x in "ilo01"}
        table = getattr(M, "both_readings", dict)()
        counts = {}
        for g, rs in table.items():
            k = "".join(sorted(name.get(r, "?") for r in rs))
            counts[k] = counts.get(k, 0) + 1
        self.assertEqual(counts, BOTH.get(UCD), "pin the computed table on this Unicode version")
        get = lambda g: "".join(sorted(name.get(r, "?") for r in table.get(g, ())))
        for gs, want in (("IΙІＩⅠ𝐈", "il"), ("1１¹𝟏", "1il"), ("0０⁰", "0o")):
            for g in gs:
                self.assertEqual(get(g), want, hex(ord(g)))
        for g in "liLoOӀ":
            self.assertNotIn(g, table, hex(ord(g)))
        probe = {"i": ("K{}m Lee", "Kim Lee"), "l": ("A{}ex Kim", "Alex Kim"), "o": ("M{}ore Kim", "Moore Kim"),
                 "1": ("XQ-{}001", "XQ-1001"), "0": ("XQ-1{}01", "XQ-1001")}
        bad = []
        for g, rs in table.items():
            have = {name.get(r) for r in rs}
            for n, (t, ident) in probe.items():
                if self.matches(t.format(g), ident) != (n in have):
                    bad.append((hex(ord(g)), n))
        self.assertEqual(bad[:10], [], f"{len(bad)} readings differ from the table")

    def test_m5_not_closed_list(self):
        for c in ("Ν", "Η", "Ɨ", "Ы", "Ꙑ"):
            self.assertIn(c, self.nc, hex(ord(c)))
        for c in ("I", "Ι", "І", "Ⅱ", "Ĳ"):
            self.assertNotIn(c, self.nc, hex(ord(c)))           # read both ways (or per letter), so not uncovered
        self.assertEqual(len(self.nc), NOT_CLOSED.get(UCD), "pin the computed list on this Unicode version")

    def test_m5_closure_over_the_tables(self):
        """Every case pair and every confusable source (letter or digit) shares the key of its other form, or, for a
        both-readings glyph, the other form is one of its readings inside a word; except the not-closed capitals. A code
        point whose NFKD is ASCII containing I, 0 or 1 is read through that NFKD (design item 1), not through its own
        prototype; a code point whose NFKC is not all word characters can never be inside a token (punctuation ends
        it) and is skipped."""
        broken = []
        for o in range(0x110000):
            c = chr(o)
            if unicodedata.category(c)[0] not in "LN" or c in self.nc:
                continue
            if not all(unicodedata.category(x)[0] in "LNM" for x in unicodedata.normalize("NFKC", c)):
                continue
            k = M.canonical(c)
            if M.canonical(k) != k:
                broken.append(("idempotent", hex(o)))
            d = unicodedata.normalize("NFKD", c)
            by_nfkd = d != c and d.isascii() and any(x in d for x in "I01")
            for other, kind in ((c.casefold(), "case"), (d if by_nfkd else self.conf.get(c), "conf")):
                if other is None or other == c or M.canonical(other) == k:
                    continue
                if M.key(f"ka{other}ka") not in M.windows(f"ka{c}ka"):
                    broken.append((kind, hex(o)))
        self.assertEqual(broken[:10], [], f"{len(broken)} code points break the closure")


class CountingKS(FakeKS):
    """FakeKS that counts every key it HMACs and the largest single batch (the operation count of the matcher)."""

    def __init__(self):
        self.total, self.max_batch, self.calls = 0, 0, 0

    def tenant_hmac_many(self, tenant, texts):
        texts = list(texts)
        self.total += len(texts)
        self.max_batch = max(self.max_batch, len(texts))
        self.calls += 1
        return super().tenant_hmac_many(tenant, texts)


class FakeConn:
    """The two queries scan() makes: the tenant's identifier-set version and matcher, and the lookup of window HMACs.
    Records the size of every lookup array."""

    def __init__(self, idents):
        self.idents, self.lookups = dict(idents), []

    def execute(self, sql, args=()):
        conn = self

        class R:
            def fetchone(self):
                return (1, None)

            def fetchall(self):
                conn.lookups.append(len(args[1]))
                return [(h, conn.idents[h]) for h in args[1] if h in conn.idents]
        return R()


# Budget m5 b1 (DESIGN-NOTES "Matcher budget (m5 b1)"); the literals, not M's names, so a red on 819258c is an assertion
PER_TOKEN, FLOOR, EVENT_MAX, MAX_BYTES, CHUNK = 512, 131072, 5_000_000, 65536, 8192
CRAFTED = " ".join("1I1I1I" for _ in range(300))            # about 4,635 keys per token: 1.39 million keys
PROSE = ("I think I will call Alison tomorrow. If Ian or Neil can join, I will book the room at 10am. "
         "Delia said the invoice INV-1001 is overdue; I asked Della to check it. ")
LEGAL = ("LIMITATION OF LIABILITY. IN NO EVENT SHALL THE LICENSOR BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, "
         "EXEMPLARY OR CONSEQUENTIAL DAMAGES INCLUDING BUT NOT LIMITED TO PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES, "
         "LOSS OF USE, DATA OR PROFITS, OR BUSINESS INTERRUPTION HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY. ")
CODE = ("```python\nfor i1 in range(10):\n    if x0 == 1 and l1 > 0:\n        Id = f(i1, l0)\n"
        "    IOError(0x10, 'I0 l1')\nclass IIlI1(Ill1):\n    def l10(self): return self.Il0 or 101\n```\n")
TABLE = ("| ID | Item | Qty | Price |\n|----|------|-----|-------|\n| I-101 | IPA lot 10 | 11 | 10.10 |\n"
         "| I-110 | Oil 1L | 100 | 1.01 |\n| L-011 | IIIx10 | 1 | 0.01 |\n")
CAPS_I = ("IMMEDIATE INITIALIZATION IS IMPOSSIBLE IN ILLINOIS; INDIVIDUALS INSISTING ON IMMINENT INSTALLATION "
          "INITIALLY IGNITED INFINITE IRRITATION. MISSISSIPPI MINIMALISM IS INFINITELY INTIMIDATING. ")
NAME = "Rosalind Okafor"


def _fill(unit, size, name=NAME):
    """unit repeated to just under `size` UTF-8 bytes, with `name` in the middle."""
    half = unit * max(1, (size // 2 - len(name) - 4) // len(unit.encode()))
    return half + " " + name + " " + half


class F12Budget(unittest.TestCase):
    """DESIGN-NOTES "Matcher budget (m5 b1)" B9: refusal of crafted text in bounded work and memory; admission of
    realistic text; the content size bound; chunked matching; the budget named in the matcher id."""

    @classmethod
    def setUpClass(cls):
        M.key("warm up the tables")                     # first use loads the pinned tables (about 2 s): not the matcher's cost

    def refused(self, call, code):
        """Run call(); return the exception, asserting its code. A call that returns is a failure (red on 819258c)."""
        try:
            call()
        except Exception as e:                           # noqa: BLE001  (the type does not exist before b1; the code is the contract)
            self.assertEqual(getattr(e, "code", None), code, f"refused with {type(e).__name__}: {e}")
            return e
        self.fail(f"not refused: expected {code}")

    def scan(self, ks, texts, names=(NAME,)):
        conn = FakeConn({h: "SUBJ" for h in M.subject_hmacs(KS, T, names)})     # the declaration is not the scan's work: not counted
        return conn, M.scan(conn, ks, T, texts)

    def test_crafted_text_refused_in_bounded_work_and_memory(self):
        import time
        import tracemalloc
        for call in (lambda ks: M.find_spans(ks, T, CRAFTED + " " + NAME, hm(NAME)),
                     lambda ks: M.scan(FakeConn({}), ks, T, ["ok", CRAFTED])):
            ks = CountingKS()
            tracemalloc.start()
            t0 = time.perf_counter()
            try:
                e = self.refused(lambda: call(ks), "WPM02")
            finally:
                dt = time.perf_counter() - t0
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            self.assertEqual(ks.total, 0, "keys were HMACed before the budget was checked")
            self.assertLess(dt, 2.0, "refusal took longer than the generous ceiling")
            self.assertLess(peak, 8 * 2 ** 20, "refusal built a structure of %d bytes" % peak)
            self.assertNotIn("1I1I1I", str(e), "the refusal repeats the content")

    def test_worst_shape_at_the_size_bound_refused_by_count(self):
        # two-letter "II" tokens: 340 keys per token, under the per-token budget, 7.4 million keys at 64 KiB: over the
        # event maximum. Only the count may run (enumerating would build millions of keys), so a matcher without a
        # count before enumeration fails here without being run
        text = " ".join(["II"] * (MAX_BYTES // 3))
        if not callable(getattr(M, "key_count", None)):
            self.fail("no key count before enumeration (m5 b1 B4)")
        n, keys = M.key_count(text)
        self.assertGreater(keys, EVENT_MAX)
        self.assertLessEqual(keys, PER_TOKEN * n)
        ks = CountingKS()
        self.refused(lambda: M.find_spans(ks, T, text, hm(NAME)), "WPM02")
        self.assertEqual(ks.total, 0)

    def test_realistic_text_admitted_and_still_matched(self):
        for label, unit, size in (("prose", PROSE, 4000), ("all-caps legal", LEGAL, 4000), ("code block", CODE, 4000),
                                  ("table", TABLE, 6000), ("long document near the bound", PROSE, MAX_BYTES - 100),
                                  ("long all-caps legal near the bound", LEGAL, MAX_BYTES - 100),
                                  ("long code near the bound", CODE, MAX_BYTES - 100)):
            text = _fill(unit, size)
            self.assertLessEqual(len(text.encode()), MAX_BYTES, label)
            ks = CountingKS()
            _, (v, found) = self.scan(ks, [text])
            self.assertEqual(found, [["SUBJ"]], label)

    def test_short_all_caps_heading_admitted_by_the_floor(self):
        for s in ("MINNEAPOLIS ILLINOIS INDIANA MISSISSIPPI MISSOURI", "INITIAL INVESTIGATION INTO MISSISSIPPI",
                  "III III III III", CAPS_I[:60]):
            _, (v, found) = self.scan(CountingKS(), [s + " " + NAME.upper()])
            self.assertEqual(found, [["SUBJ"]], s)

    def test_long_i_heavy_capitals_refused_stated_limitation(self):
        self.refused(lambda: self.scan(CountingKS(), [CAPS_I * 10]), "WPM02")      # B8: 309,286 keys for 200 tokens

    def test_content_size_bound(self):
        at = "a" * MAX_BYTES
        _, (v, found) = self.scan(CountingKS(), [at])
        self.assertEqual(found, [[]])
        for over in ("a" * (MAX_BYTES + 1), "\U0001F600" * (MAX_BYTES // 4 + 1), "é" * (MAX_BYTES // 2 + 1)):
            ks = CountingKS()
            e = self.refused(lambda: self.scan(ks, ["fine", over]), "WPM01")
            self.assertEqual(ks.total, 0)
            self.assertEqual(getattr(e, "index", None), 1, "the refusal names the event in the batch")
        self.refused(lambda: M.find_spans(CountingKS(), T, "a" * (MAX_BYTES + 1), hm(NAME)), "WPM01")
        _, (v, found) = self.scan(CountingKS(), ["\U0001F600" * (MAX_BYTES // 4)])          # exactly the bound in bytes
        self.assertEqual(found, [[]])

    def test_admitted_heavy_text_is_matched_in_chunks_within_its_budget(self):
        # 20 distinct tokens of 8 readings each: about 93,600 distinct keys, under the floor (admitted)
        toks = ["III" + c for c in "abcdefghjkmnpqrstuvw"]
        text = " ".join(toks) + " " + NAME
        ks = CountingKS()
        conn, (v, found) = self.scan(ks, [text])
        self.assertEqual(found, [["SUBJ"]])
        self.assertLessEqual(ks.total, FLOOR + 2, "more keys HMACed than the budget")
        self.assertLessEqual(ks.max_batch, CHUNK, "one HMAC batch held more than a chunk")
        self.assertTrue(conn.lookups and max(conn.lookups) <= CHUNK, f"lookup arrays {conn.lookups[:5]}")
        ks = CountingKS()
        self.assertEqual(M.find_spans(ks, T, text, hm(NAME)) != [], True)
        self.assertLessEqual(ks.max_batch, CHUNK)

    def test_count_equals_enumeration(self):
        # the bound rests on this: the arithmetic count is exactly the number of keys enumerated (repeats included), and
        # a token's reading count is exactly its number of distinct keys (so the cap skips the same windows as before b1)
        if not callable(getattr(M, "key_count", None)):
            self.fail("no key count before enumeration (m5 b1 B4)")
        glyphs = ["I", "l", "i", "1", "0", "o", "O", "Ⅱ", "Ⅲ", "ĳ", "Ĳ", "Ｉ", "Ι", "ⅰ",
                  "¹", "①", "a", "n", "m", "rn", "e", "́", "​"]
        r = random.Random(16096)
        for _ in range(500):
            s = " ".join("".join(r.choice(glyphs) for _ in range(r.randint(1, 9))) for _ in range(r.randint(1, 6)))
            self.assertEqual(len(M._windows_at(s)), M.key_count(s)[1], repr(s))
            for tok in M._tokens(s):
                tk = M._token_keys(tok)
                self.assertEqual(None if tk is None else len(tk), M._reading_count(tok), repr(tok[0]))

    def test_budget_is_named_in_the_matcher_id(self):
        self.assertIn(":b%d.%d.%d.%d." % (PER_TOKEN, FLOOR, EVENT_MAX, MAX_BYTES), M.matcher_id())

    def test_store_bounds_ciphertext_size(self):
        sql = (HERE / "wp" / "schema.sql").read_text()
        for table in ("events", "quarantine"):
            pat = rf"constraint {table}_content_size check \([^)]*octet_length\(content_enc\) <= {MAX_BYTES + 28}\)"
            self.assertIsNotNone(re.search(pat, sql), f"{table}: no ciphertext size bound of {MAX_BYTES} + 28 (nonce and tag)")


class F12StopClass(unittest.TestCase):
    """DESIGN-NOTES item 11, the rule stated: an identifier that would match a phrase made only of common words, in lower
    case, capitalised or capitals, is refused; generated with both readings applied (i, l and o replaced by I, l, i, 1,
    0 and O). An independent oracle: windows() of the phrase, not the rule's own stop keys."""

    SUBS = {"i": "iIl1", "l": "lIi1", "o": "oO0"}
    OTHER = ("the", "it", "is", "in", "of", "to")       # the second word of each phrase

    def variants(self, w):
        opts = [self.SUBS.get(c, c) for c in w]
        out = [""]
        for o in opts:
            out = [p + x for p in out for x in o][:256]
        return out

    def test_s1_stop_word_with_both_readings_refused(self):
        checked, accepted = 0, []
        for w in sorted(M.STOP):
            pairs = [(f"{v} {o}", f"{w} {o}") for o in self.OTHER for v in self.variants(w)]
            pairs += [(f"{o} {v}", f"{o} {w}") for o in self.OTHER for v in self.variants(w)]
            seen = {}
            for _, phrase in pairs:
                if phrase not in seen:
                    seen[phrase] = set().union(*(M.windows(f) for f in (phrase, phrase.capitalize(), phrase.upper(), phrase.title())))
            for ident, phrase in pairs:
                if M.key(ident) not in seen[phrase]:
                    continue
                checked += 1
                if M.identifier_problem(ident) is None:
                    accepted.append(ident)
        self.assertGreater(checked, 500, "the generator checked too few identifiers to mean anything")
        self.assertEqual(accepted[:10], [], f"{len(accepted)} identifiers matching common-word phrases were accepted")


CALL_EVENTS, CALL_BYTES, CALL_KEYS = 1000, 1_048_576, 5_000_000
FLOOR_SHAPE = " ".join(["IIIa", "IIIb", "IIIc", "IIId"] * 7 + ["IIIe"])     # 144 bytes, 122,344 keys: under the floor


class F13CallBudget(unittest.TestCase):
    """DESIGN-NOTES "Matcher budget (m5 b1)" B10 (main's decision 1): one call (one append, one scan) is bounded too:
    at most CALL_EVENTS events, CALL_BYTES bytes and CALL_KEYS keys in all, refused with WPM03 before any HMAC."""

    @classmethod
    def setUpClass(cls):
        M.key("warm up the tables")

    refused = F12Budget.refused

    def admitted(self, texts):
        """Admission only (the count, no enumeration): a call at the limits is not refused."""
        try:
            M.admit(texts)
        except Exception as e:                           # noqa: BLE001
            self.fail(f"refused at the limit: {getattr(e, 'code', None)} {e}")

    def test_call_event_count(self):
        ks = CountingKS()
        e = self.refused(lambda: M.scan(FakeConn({}), ks, T, ["x"] * (CALL_EVENTS + 1)), "WPM03")
        self.assertEqual(ks.total, 0)
        self.assertIn(str(CALL_EVENTS), str(e))
        self.admitted([""] * CALL_EVENTS)

    def test_call_bytes(self):
        ks = CountingKS()
        e = self.refused(lambda: M.scan(FakeConn({}), ks, T, ["a" * 64000] * 17), "WPM03")      # 1,088,000 bytes
        self.assertEqual(ks.total, 0)
        self.assertEqual(getattr(e, "index", None), 16, "the refusal names the event that crossed the limit")
        self.admitted(["a" * 64000] * 16)

    def test_call_keys_many_floor_shaped_events(self):
        # each event is under its own budget (the floor); 41 of them are 5,016,104 keys in 5.9 KB: over the call limit
        self.assertEqual(M.key_count(FLOOR_SHAPE), (29, 122344))
        import time
        ks = CountingKS()
        t0 = time.perf_counter()
        self.refused(lambda: M.scan(FakeConn({}), ks, T, [FLOOR_SHAPE] * 41), "WPM03")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(ks.total, 0)
        self.admitted([FLOOR_SHAPE] * 40)                # 4,893,760 keys

    def test_call_limits_named_in_the_matcher_id(self):
        self.assertTrue(M.matcher_id().endswith(":b%d.%d.%d.%d.%d.%d.%d.%d" % (PER_TOKEN, FLOOR, EVENT_MAX, MAX_BYTES, CALL_EVENTS, CALL_BYTES, CALL_KEYS, 64)),
                        M.matcher_id())

    def test_store_counts_the_call_code(self):
        sql = (HERE / "wp" / "schema.sql").read_text()
        self.assertIsNotNone(re.search(r"code text not null check \(code in \('WPM01', 'WPM02', 'WPM03'\)\)", sql), "write_refusals does not accept WPM03")
        self.assertIsNotNone(re.search(r"c not in \('WPM01', 'WPM02', 'WPM03'\)", sql), "wp_write_refused does not accept WPM03")


TOKEN_CP = 64                       # review H H1: a token is at most 64 code points, written and as key (BUDGET.token_cp)
CEILING = 64 * 2**20                # review H H1: one call holds at most about 46 MiB besides the fixed tables
MAX_IDENTS, MAX_IDENT_BYTES = 64, 256


def traced(fn):
    """(result, peak traced bytes, seconds) of fn() (tables are loaded before, so only the call's own memory counts)."""
    import time
    import tracemalloc
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        out = fn()
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return out, peak, time.perf_counter() - t0


class CanonicalSpy:
    """Context manager: records the length of every string M.canonical() is asked to normalise."""

    def __enter__(self):
        self.orig, self.lengths = M.canonical, []

        def spy(s):
            self.lengths.append(len(s))
            return self.orig(s)
        M.canonical = spy
        return self

    def __exit__(self, *exc):
        M.canonical = self.orig
        return False


class H13Resource(unittest.TestCase):
    """DESIGN-NOTES "Review H" H1, H2 (F13): derived bytes are bounded by admitted bytes and one structural constant
    (a token is at most TOKEN_CP code points); the worst admitted shapes hold less than CEILING."""

    @classmethod
    def setUpClass(cls):
        M.key("warm up the tables")

    def test_f13_shape_builds_no_readings(self):
        # one 65,536-byte token with 4,096 readings: 6ddeca3 built 4,096 keys of 65,536 code points (518 MiB traced)
        ks = CountingKS()
        _, peak, _ = traced(lambda: M.scan(FakeConn({}), ks, T, ["I" * 12 + "a" * 65524]))
        self.assertEqual(ks.total, 0, "readings of an over-long token were HMACed")
        self.assertLess(peak, CEILING, "peak %.1f MiB" % (peak / 2**20))

    def test_long_cluster_is_not_normalised(self):
        # one cluster of 32,768 code points (a base and alternating combining classes): cut at TOKEN_CP, not normalised
        M._CLUSTERS.clear()
        with CanonicalSpy() as spy:
            M.key_count("I" + "̖́" * 16383)
        self.assertLessEqual(max(spy.lengths or [0]), 4 * TOKEN_CP, "a cluster longer than the token cap was normalised")

    def test_over_long_token_has_no_keys(self):
        self.assertIsNone(M._reading_count(M._tokens("a" * (TOKEN_CP + 1))[0]))
        self.assertEqual(M._reading_count(M._tokens("a" * TOKEN_CP)[0]), 1)
        self.assertEqual(M.key_count("a" * TOKEN_CP + " " + "b" * (TOKEN_CP + 1)), (2, 1))
        self.assertEqual(M.windows("a" * TOKEN_CP + " " + "b" * (TOKEN_CP + 1)), {"a" * TOKEN_CP})

    def test_count_equals_enumeration_with_over_long_tokens(self):
        glyphs = ["I", "l", "1", "0", "a", "n", "m", "́", "​", "Ⅲ"]
        r = random.Random(1609613)
        for _ in range(200):
            s = " ".join("".join(r.choice(glyphs) for _ in range(r.choice((1, 3, 30, 63, 64, 65, 70))))
                         for _ in range(r.randint(1, 5)))
            self.assertEqual(len(M._windows_at(s)), M.key_count(s)[1], repr(s[:40]))

    def test_every_reading_has_its_key_length(self):
        # the window-key bound (4 x TOKEN_CP + 3 code points) rests on this: a reading replaces one character by one
        for g in M.both_readings():
            for cl in (g, g + "́", g + "̖́"):
                k, full, lit = M._cluster(cl)
                self.assertTrue(all(len(x) == len(k) for x in full + lit), repr(cl))

    def test_identical_events_peak(self):
        # 1,000 identical events: every pending key carried 1,000 tags (123 MiB traced at 6ddeca3)
        unit = " ".join("II" + c for c in "abcdefghjkmnpq")
        n, k = M.key_count(unit)
        ks = CountingKS()
        _, peak, _ = traced(lambda: M.scan(FakeConn({}), ks, T, [unit] * min(CALL_EVENTS, CALL_KEYS // k)))
        self.assertLessEqual(ks.max_batch, CHUNK)
        self.assertLess(peak, CEILING, "peak %.1f MiB" % (peak / 2**20))

    def test_cluster_cache_is_bounded_in_bytes(self):
        limit = 64 * 1024
        had = hasattr(M, "CLUSTER_CACHE_BYTES")
        old = getattr(M, "CLUSTER_CACHE_BYTES", None)
        M.CLUSTER_CACHE_BYTES = limit
        M._CLUSTERS.clear()
        try:
            for i in range(2000):                        # about 2,000 distinct clusters (128 bases x 16 mark runs)
                M.key_count(chr(0x100 + i % 128) + "".join(chr(0x300 + (j * 7 + i // 128) % 0x70) for j in range(29)))
            size = sum(sys.getsizeof(c) + sum(sys.getsizeof(x) for x in (v[0],) + tuple(v[1]) + tuple(v[2]))
                       for c, v in M._CLUSTERS.items())
            self.assertLessEqual(size, limit + 4096, "cluster cache %d bytes over a limit of %d" % (size, limit))
        finally:
            if had:
                M.CLUSTER_CACHE_BYTES = old
            else:
                del M.CLUSTER_CACHE_BYTES
            M._CLUSTERS.clear()

    def test_token_cap_named_in_the_matcher_id(self):
        self.assertTrue(M.matcher_id().endswith(".%d.%d.%d.%d" % (CALL_EVENTS, CALL_BYTES, CALL_KEYS, TOKEN_CP)), M.matcher_id())


class H15Identifiers(unittest.TestCase):
    """DESIGN-NOTES "Review H" H4 (F15): identifier count and bytes are refused before any normalisation; an over-long
    token is refused; capacity saturates."""

    @classmethod
    def setUpClass(cls):
        M.key("warm up the tables")

    @staticmethod
    def names(n):
        return ["Rosalind Okafor" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(n)]

    def refused_unnormalised(self, idents):
        with CanonicalSpy() as spy:
            try:
                M.check_identifiers(idents)
            except M.IdentifierRefused as e:
                self.assertEqual(spy.lengths, [], "normalised before the size refusal")
                return str(e)
        self.fail("accepted")

    def test_identifier_count(self):
        why = self.refused_unnormalised(self.names(MAX_IDENTS + 1))
        self.assertIn(str(MAX_IDENTS), why)
        self.assertEqual(len(M.check_identifiers(self.names(MAX_IDENTS))), MAX_IDENTS)

    def test_identifier_bytes(self):
        long = "b" * 128 + " " + "c" * 128                                           # 257 bytes
        why = self.refused_unnormalised([long])
        self.assertIn(str(MAX_IDENT_BYTES), why)
        self.assertIsNotNone(M.identifier_problem(long))                             # the reviewer path too
        self.assertIsNone(M.identifier_problem("b" * 60 + " " + "c" * 60))

    def test_over_long_token_refused(self):
        self.assertIsNotNone(M.identifier_problem("b" * (TOKEN_CP + 1)))
        self.assertIsNone(M.identifier_problem("b" * TOKEN_CP))

    def test_capacity_saturates(self):
        self.assertLessEqual(M.capacity("I" * 60), 9 * CAP, "capacity computed the whole product")
        self.assertGreater(M.capacity("I" * 13), CAP)
        self.assertIsNotNone(M.identifier_problem("I" * 13))


OVER = CRAFTED                                       # 300 tokens, about 1.39 million keys: over its budget of 153,600


class H14Unscannable(unittest.TestCase):
    """DESIGN-NOTES "Review H" H3 (F14, Class B): the erasure rescan classifies an event it cannot scan within budget as
    unscannable, by the count alone (no key enumerated), instead of raising; admitted text is never unscannable."""

    @classmethod
    def setUpClass(cls):
        M.key("warm up the tables")

    def classify(self, items, hmacs, ks=None):
        uns = []
        try:
            got = M.stream_matches(ks or CountingKS(), T, items, hmacs, unscannable=uns)
        except TypeError as e:
            self.fail(f"stream_matches cannot classify unscannable events: {e}")
        except M.WriteRefused as e:
            self.fail(f"an unscannable event raised {e.code} instead of being classified")
        return got, uns

    def test_over_budget_and_oversize_are_classified_without_keys(self):
        ks = CountingKS()
        near = "x " + NAME
        got, uns = self.classify([(0, OVER), (1, near), (2, "a" * (MAX_BYTES + 1))], hm(NAME), ks)
        self.assertEqual(got, {1})
        self.assertEqual(uns, [0, 2])
        self.assertLessEqual(ks.total, M.key_count(near)[1], "keys of an unscannable event were HMACed")

    def test_admitted_text_is_never_unscannable(self):
        r = random.Random(16096014)
        glyphs = ["I", "l", "i", "1", "0", "o", "a", "Ⅲ", "́", " ", " "]
        texts = [PROSE, LEGAL, CODE, TABLE, CAPS_I[:400], FLOOR_SHAPE, "a" * 70 + " " + NAME]
        texts += ["".join(r.choice(glyphs) for _ in range(r.randint(1, 120))) for _ in range(200)]
        texts = [x for x in texts if self.admits(x)]
        self.assertGreater(len(texts), 150)
        _, uns = self.classify(list(enumerate(texts)), hm(NAME))
        self.assertEqual(uns, [])

    @staticmethod
    def admits(x):
        try:
            M.admit([x])
            return True
        except M.WriteRefused:
            return False

    def test_without_a_list_it_still_raises(self):
        with self.assertRaises(M.WriteRefused):
            M.stream_matches(CountingKS(), T, [(0, OVER)], hm(NAME))


def _fn_body(sql, name):
    m = re.search(rf"create function {name}\(.*?\nend \$\$;", sql, re.S)
    return m.group(0) if m else ""


class H14Schema(unittest.TestCase):
    """DESIGN-NOTES "Review H" H1, H3, H5, H6, H7: the store-side bounds and rules, read from the schema."""

    @classmethod
    def setUpClass(cls):
        cls.sql = (HERE / "wp" / "schema.sql").read_text()

    def has(self, pat, why):
        self.assertIsNotNone(re.search(pat, self.sql, re.S), why)

    def test_python_only_sizes_are_sql_constraints(self):
        self.has(r"constraint inbox_payload_size check \(octet_length\(payload_enc\) <= 65564\)", "inbox payload unbounded")
        self.has(r"constraint analyst_jobs_result_size check \(result_enc is null or octet_length\(result_enc\) <= 65564\)", "job result unbounded")
        self.has(r"constraint identifier_proposals_size check \(octet_length\(ident_enc\) <= 284\)", "proposal ciphertext unbounded")
        self.assertIn("WPS39", _fn_body(self.sql, "wp_identifier_propose"), "open proposals per subject unbounded")

    def test_events_are_append_only_for_the_app(self):
        # verified, not assumed (H3): only an insert can put content in events, and only the cascade changes it
        for g in re.findall(r"grant ([^;]*?) to ([^;]*);", self.sql, re.S):
            privs, rest = g[0].split(" on ", 1) if " on " in g[0] else (g[0], "")
            if "wp_app" in g[1] and "update" in privs and not rest.startswith("function"):
                self.assertNotRegex(rest, r"\bevents\b", "wp_app may update events")
        self.assertEqual(set(re.findall(r"update events set ([a-z_]+ = [a-z]+)", self.sql)), {"content_enc = null"})

    def test_unscannable_is_a_mention_source(self):
        self.has(r"source in \('write', 'rescan', 'unscannable'\)", "erasure_mentions has no unscannable source")
        self.has(r"grant execute on function [^;]*wp_erasure_add_unscannable\(text, text\[\]\)[^;]* to wp_app", "no unscannable link for the runner")
        self.assertNotIn("CANNOT COMPLETE", self.sql, "the receipt still says such an erasure cannot complete")
        self.assertIn("'unscannable_removed'", self.sql)

    def test_hold_placement_does_no_footprint_work(self):
        body = _fn_body(self.sql, "wp_hold_place")
        self.assertNotIn("array_agg", body)
        self.assertNotIn("count(", body)
        self.assertIn("pg_current_snapshot()", body)
        self.has(r"removed_xid xid8 not null default pg_current_xact_id\(\)", "tombstones carry no removal xid")
        self.has(r"grant execute on function [^;]*wp_hold_report\(text\)[^;]* to wp_legal", "no separate hold report")
        report = _fn_body(self.sql, "wp_hold_report")
        self.assertIn("limit 20", report)
        self.assertIn("limit 10001", report)
        self.assertIn("pg_visible_in_snapshot", report)

    def test_receipt_id_lists_are_bounded(self):
        body = _fn_body(self.sql, "wp_erasure_complete")
        for key in ("other_speakers_removed", "unscannable_removed"):
            m = re.search(rf"'{key}', (.{{0,700}})", body, re.S)
            self.assertIsNotNone(m, key)
            self.assertIn("limit 1000", m.group(1), f"{key} aggregates every id")

    def test_minors(self):
        self.assertNotIn("one lock order everywhere", self.sql)
        self.assertEqual(M.key("AB-1001"), M.key("AB/1001"))
        self.assertTrue(any("AB-1001" in x for x in M.OVER_MATCH), "separator collisions are not catalogued")
        self.assertIn("AB-1001", self.sql, "the receipt does not state separator collisions")


if __name__ == "__main__":
    unittest.main()
