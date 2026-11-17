"""Offline unit test (no stores) for D3 rule r3 and matcher m4 (wp/mentions.py), DESIGN-NOTES "Redesign after review C"
classes 3 and 4 and "Redesign after review E" F11 (scenario ids S1..S16 refer to "Design: D3"). ASSUMPTION: decision 10
and decision R1 (no span redaction; whole-event removal only).

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
        self.assertRegex(mid, rf"^m4:ucd{re.escape(UCD)}:di[0-9a-f]{{12}}:cf[0-9a-f]{{12}}$")
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
        self.assertEqual(M.key("ALEX KIM"), M.key("A1ex Kirn"))
        self.assertEqual(hm("Alex Kim"), hm("ａｌｅｘ kirn"))

    def test_s6_capital_shape_lookalike_has_the_same_single_key(self):
        # m4 (review E F11): one key per token; capital I (look-alike of l) and small i share a class with l
        self.assertEqual(M.key("AIex Kim"), M.key("Alex Kim"))
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


class F11OneCanonicalKey(unittest.TestCase):
    """Review E F11: case changes and look-alike substitutions may be MIXED inside one token; one canonical key per
    token (matcher m4) must find every such variant of an approved identifier, and nothing outside the stated classes.
    The generator takes its substitutions from the RAW confusables table, not from the matcher's classes."""
    IDENTS = ("Alison", "Alex Kim", "Zoë Renée", "Marta Straße", "Nguyệt Lê", "Александр Ким", "Ηλίας Παππάς",
              "Kimberly Moore", "XQ-1001")
    SEED, PER_IDENT = 16096, 300

    @classmethod
    def setUpClass(cls):
        di, conf = M.load_tables(UCD_DIR)
        cls.di = sorted(di)
        cls.conf = {chr(k): v for k, v in conf.items()}
        cls.by_proto = {}
        for s, p in cls.conf.items():
            if unicodedata.category(s)[0] in "LN" and len(unicodedata.normalize("NFKD", s)) == 1:
                cls.by_proto.setdefault(p, []).append(s)

    def matches(self, text, ident):
        return len(found(f"please ask {text} today", ident)) == 1

    def test_f11_reviewer_counterexample(self):
        # one capital I is the look-alike of l, the other the capital of i (review E F11)
        for v in ("AIIson", "AlIson", "aLIsOn", "ALISON", "A1ison"):
            self.assertEqual(M.key(v), M.key("Alison"), v)
            self.assertTrue(self.matches(v, "Alison"), v)

    def test_f11_one_key_per_token(self):
        # m3 looked a window up under every combination of two keys per token; m4 has one key, so one window each
        self.assertEqual(len(M._windows_at("AIex AIIson KIM")), 6)

    def test_f11_ascii_classes(self):
        cls = {}
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
            cls.setdefault(M.key(ch), []).append(ch)
            if ch.isalpha():
                self.assertEqual(M.key(ch), M.key(ch.upper()), ch)
        self.assertEqual(sorted("".join(v) for v in cls.values() if len(v) > 1), ["il1", "o0"])

    def variant(self, rnd, ident):
        out = []
        for c in unicodedata.normalize("NFC", ident):
            r = rnd.random()
            if r < 0.25 and c.isalpha():
                c = c.swapcase() if len(c.swapcase()) == 1 else c
            elif r < 0.55 and c.isalpha():
                nc = set(getattr(M, "not_closed", tuple)())     # absent before m4: the red shows the misses
                # a capital whose case pair is not closed is matched by its small form: its look-alikes are the small
                # form's (the published exception, NOT_COVERED)
                vs = {c, c.upper(), c.lower()} if not ({c, c.upper()} & nc) else {c.casefold()}
                src = sorted({s for v in vs if len(v) == 1 for s in self.by_proto.get(self.conf.get(v, v), ())} - nc)
                if src:
                    c = rnd.choice(src)
            out.append(c)
            if rnd.random() < 0.15:
                out.append(chr(rnd.choice(self.di)))
        v = "".join(out)
        return unicodedata.normalize(rnd.choice(("NFC", "NFD")), v)

    def test_f11_generated_variants_match(self):
        rnd, missed, n = random.Random(self.SEED), [], 0
        for ident in self.IDENTS:
            for _ in range(self.PER_IDENT):
                v = self.variant(rnd, ident)
                n += 1
                if not self.matches(v, ident):
                    missed.append((ident, v, [hex(ord(c)) for c in v]))
        self.assertEqual(missed[:5], [], f"{len(missed)} of {n} generated variants not matched")

    def test_f11_reordered_combining_marks(self):
        # e + dot below (ccc 220) + circumflex (230) in either order is canonically equivalent (UAX 15)
        self.assertTrue(self.matches("Nguyệt Lê", "Nguyệt Lê"))
        self.assertTrue(self.matches("NGUYỆT LÊ", "Nguyệt Lê"))

    def test_f11_near_misses_do_not_match(self):
        merged = {c: g for g in ("il1", "o0") for c in g}
        letters = "abcdefghijklmnopqrstuvwxyz"
        bad = []
        for ident in ("Alison", "Alex Kim", "Kimberly Moore"):
            for i, c in enumerate(ident):
                if not c.isalpha():
                    continue
                for x in letters:
                    if x in merged.get(c.lower(), c.lower()):
                        continue
                    for v in (ident[:i] + x + ident[i + 1:], ident[:i] + x.upper() + ident[i + 1:]):
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

    def test_f11_stated_over_match(self):
        for v in ("Allson", "Aiison", "Ailson", "A1ison", "AIis0n"):
            self.assertTrue(self.matches(v, "Alison"), v)
        self.assertTrue(any("Allson" in x for x in M.OVER_MATCH))
        # stated (review E advisor check): real names that differ only by i and l share a key under m4
        for a, b in (("Delia Smith", "Della Smith"), ("Hailey Brown", "Halley Brown"), ("Neil Young", "Nell Young")):
            self.assertTrue(self.matches(b, a), (a, b))
        self.assertTrue(any("Della" in x for x in M.OVER_MATCH))
        self.assertTrue(self.matches("lan Smith", "Ian Smith"))             # no longer in NOT_COVERED
        self.assertFalse(any("lan for a declared Ian" in x for x in M.NOT_COVERED))

    def test_f11_closure_over_the_tables(self):
        """Every case pair that the matcher declares closed shares the key; every confusable source (letter or digit)
        shares the key of its prototype, except the published not-closed capitals."""
        nc = set(M.not_closed())
        self.assertIn("Ν", nc)                                       # Greek capital Nu: its small form looks like v
        self.assertLess(len(nc), 400, "the not-closed list is a short published exception, not the rule")
        broken = []
        for o in range(0x110000):
            c = chr(o)
            if unicodedata.category(c)[0] not in "LN" or c in nc:
                continue
            k = M.canonical(c)
            if M.canonical(k) != k:
                broken.append(("idempotent", hex(o)))
            f = c.casefold()
            if f != c and k != M.canonical(f):
                broken.append(("case", hex(o)))
            p = self.conf.get(c)
            if p is not None and M.canonical(c) != M.canonical(p):
                broken.append(("conf", hex(o)))
        self.assertEqual(broken[:10], [], f"{len(broken)} code points break the closure")


if __name__ == "__main__":
    unittest.main()
