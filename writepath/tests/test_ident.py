"""Offline unit test (no stores) for D3 rule r3 and matcher m3 (wp/mentions.py), DESIGN-NOTES "Redesign after review C"
classes 3 and 4 (scenario ids S1..S16 refer to "Design: D3"). ASSUMPTION: decision 10 and decision R1 (no span
redaction; whole-event removal only).

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
        self.assertRegex(mid, rf"^m3:ucd{re.escape(UCD)}:di[0-9a-f]{{12}}:cf[0-9a-f]{{12}}$")
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


class MatcherM3(unittest.TestCase):
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

    def test_s6_skeleton_is_the_only_key(self):
        self.assertEqual(M.key("Alex Kim"), M.key("AIex Kirn"))
        self.assertEqual(hm("Alex Kim"), hm("A1ex Kirn"))

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


if __name__ == "__main__":
    unittest.main()
