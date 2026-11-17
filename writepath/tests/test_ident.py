"""Offline unit test (no stores) for D3 rule r2 and matcher m2 (wp/mentions.py), DESIGN-NOTES "Design: D3".
Scenario ids (S1..S16) refer to the design note. ASSUMPTION: decision 10 (identifier rule plus span redaction).
A fake key service HMACs with a fixed test key, so matching works exactly as with the real tenant key.
usage (from writepath/): python3 -m unittest tests.test_ident -v"""
import hashlib
import hmac
import importlib
import pathlib
import sys
import unicodedata
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
M = importlib.import_module("wp.mentions")


class FakeKS:
    KEY = b"test-only-fixed-key"

    def tenant_hmac_many(self, tenant, texts):
        return [hmac.new(self.KEY, f"{tenant}|{x}".encode(), hashlib.sha256).hexdigest() for x in texts]


KS, T = FakeKS(), "t1"


def hm(*idents):
    return M.subject_hmacs(KS, T, idents)


def spans(text, *idents):
    return M.find_spans(KS, T, text, hm(*idents))


class RuleR2(unittest.TestCase):
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

    def test_s5_mixed_script_declared_identifier_refused(self):
        self.bad("Аlexander Kim", "script")          # Cyrillic capital A inside a Latin name
        self.bad("Aleхander", "script")              # Cyrillic small ha
        self.ok("Александр Ким")   # all Cyrillic

    def test_s16_unsegmented_script_refused(self):
        self.bad("山田太郎", "spaces")     # Han
        self.bad("สมชาย", "spaces")  # Thai

    def test_unassigned_code_point_refused(self):
        self.bad("Alex͸ander Kim", "unassigned")

    def test_s3_non_ascii_letters_stay_in_the_token(self):
        self.assertEqual(M.normalize("José García"), "josé garcía")
        self.assertEqual(M.normalize("JOSÉ GARCÍA"), "josé garcía")   # combining acute -> NFKC composed
        self.assertEqual(M.normalize("Marta STRASSE"), M.normalize("marta straße"))   # casefold
        self.assertEqual(M.normalize("Ａｌｅｘ Kim"), "alex kim")          # fullwidth -> NFKC

    def test_s4_ignorables_removed_from_declared_identifier(self):
        self.assertEqual(M.normalize("Al‍ex K​im"), "alex kim")
        self.assertEqual(M.normalize("Alex️ Kim"), "alex kim")

    def test_s15_hmac_label_binds_matcher_and_unicode_version(self):
        self.assertEqual(M.LABEL, f"ident:m2:ucd{unicodedata.unidata_version}:")
        self.assertEqual(M.subject_hmacs(KS, T, ["Alex Kim"]), set(KS.tenant_hmac_many(T, [M.LABEL + "alex kim"])))


class LabelInOnePlace(unittest.TestCase):
    def test_s15_no_module_builds_identifier_hmacs_itself(self):
        """A second copy of the label (erasure's rescan had "ident:" + w) silently stops matching when the label
        changes: every identifier HMAC must come from wp/mentions.py."""
        offenders = [p.name for p in (HERE / "wp").glob("*.py")
                     if p.name != "mentions.py" and ('"ident:' in p.read_text() or "'ident:" in p.read_text())]
        self.assertEqual(offenders, [])


class MatcherM2(unittest.TestCase):
    def test_s2_whole_tokens_only(self):
        self.assertEqual(spans("Alex Kimball called", "Alex Kim"), [])
        t = "Alex Kim's car"
        self.assertEqual([t[a:b] for a, b in spans(t, "Alex Kim")], ["Alex Kim"])

    def test_s3_spans_are_offsets_in_the_original_text(self):
        for t, ident, want in (("call ALEX KIM now", "Alex Kim", "ALEX KIM"),
                               ("Ａｌｅｘ Ｋｉｍ!", "Alex Kim", "Ａｌｅｘ Ｋｉｍ"),
                               ("ask JOSÉ GARCÍA.", "José García", "JOSÉ GARCÍA"),
                               ("Marta STRASSE wrote", "Marta Straße", "Marta STRASSE")):
            got = [t[a:b] for a, b in spans(t, ident)]
            self.assertEqual(got, [want], (t, ident))

    def test_s4_invisible_characters_inside_a_name_in_text(self):
        t = "met Al‍ex K​im today"
        self.assertEqual([t[a:b] for a, b in spans(t, "Alex Kim")], ["Al‍ex K​im"])

    def test_s7_identifier_inside_quoted_text_of_another_speaker(self):
        t = 'Zoe wrote: "please call Alex Kim tomorrow"'
        self.assertEqual([t[a:b] for a, b in spans(t, "Alex Kim")], ["Alex Kim"])

    def test_two_occurrences_and_overlap_merged(self):
        t = "Alex Kim and alex kim; XQ-1001"
        got = spans(t, "Alex Kim", "XQ-1001", "Kim XQ")
        # "Kim XQ" spans "kim; XQ" (windows ignore punctuation), overlapping both neighbours: overlaps are merged
        self.assertEqual([t[a:b] for a, b in got], ["Alex Kim", "alex kim; XQ-1001"])
        self.assertTrue(all(got[i][1] <= got[i + 1][0] for i in range(len(got) - 1)), "ordered, non-overlapping")

    def test_s6_text_side_lookalike_is_not_matched_and_is_disclosed(self):
        t = "call Аlex Kim"                                   # Cyrillic A: needs the UTS 39 skeleton
        self.assertEqual(spans(t, "Alex Kim"), [])
        self.assertIn("look-alike", " ".join(M.NOT_COVERED))


class Redaction(unittest.TestCase):
    def test_s8_redaction_is_built_from_spans_and_rescanned(self):
        t = 'Zoe: "call Alex Kim", Alex Kim agreed'
        red, sp = M.redaction_for(KS, T, t, hm("Alex Kim"))
        self.assertEqual(red, 'Zoe: "call ' + M.MARKER + '", ' + M.MARKER + " agreed")
        self.assertEqual(len(sp), 2)
        self.assertEqual(M.find_spans(KS, T, red, hm("Alex Kim")), [])
        self.assertIsNotNone(M.identifier_problem(M.normalize(M.MARKER)), "the marker can never be an identifier")

    def test_s8_no_span_means_no_redaction(self):
        with self.assertRaises(M.NoRedaction):
            M.redaction_for(KS, T, "nothing to see", hm("Alex Kim"))

    def test_s8_residual_hit_means_no_redaction(self):
        # spans that do not cover the identifier (offset drift) must never produce a redaction
        with self.assertRaises(M.NoRedaction):
            M.apply_spans(KS, T, "Alex Kim here", [(0, 2)], hm("Alex Kim"))

    def test_s8_spans_validated(self):
        for bad in ([], [(3, 3)], [(4, 8), (0, 5)], [(0, 5), (4, 8)], [(-1, 2)], [(0, 99)]):
            with self.assertRaises(M.NoRedaction, msg=str(bad)):
                M.apply_spans(KS, T, "Alex Kim here", bad, hm("Alex Kim"))

    def test_s12_second_subject_on_redacted_text(self):
        t = "Alex Kim met Bea Novak"
        red1, _ = M.redaction_for(KS, T, t, hm("Alex Kim"))
        red2, _ = M.redaction_for(KS, T, red1, hm("Bea Novak"))
        self.assertEqual(red2, M.MARKER + " met " + M.MARKER)


if __name__ == "__main__":
    unittest.main()
