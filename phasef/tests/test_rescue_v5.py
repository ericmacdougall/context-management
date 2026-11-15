import unittest
from datetime import datetime

from phasef import links as L
from phasef.rescue_v5 import candidates_v5, lemma_v5, parse_verify, tokset_v5, verified_label


def F(slot, obj, day, sid, mod="asserted", pred="p", subj="user", cues=()):
    return {"slot": slot, "predicate": pred, "subject": subj, "object": obj, "asserted_at": datetime(2023, 1, day), "modality": mod, "cardinality": "single",
            "author": "user", "scope": "personal", "session_id": sid, "session_key": sid + "k", "round": 0, "order": day, "quote": "q", "cue_keys": list(cues)}


BASE = {"theta": 0.12, "per_fact": 5, "max_pairs": 40, "protect": 0, "fix": True}


class Lemma(unittest.TestCase):
    def test_stored_matches_store_only_with_the_fix(self):
        self.assertEqual(lemma_v5("stored"), lemma_v5("store"))
        self.assertNotEqual(L.lemma("stored"), L.lemma("store"))  # the v4 defect this fixes (07741c45)
        self.assertEqual(lemma_v5("shoes"), L.lemma("shoes"))
        self.assertEqual(lemma_v5("visited"), L.lemma("visited"))

    def test_tokset_fix_flag(self):
        f = F("old_sneakers.storage_location", "bed", 1, "s1", pred="are stored")
        self.assertIn("store", tokset_v5(f, True))
        self.assertNotIn("store", tokset_v5(f, False))


class CandidatesV5(unittest.TestCase):
    def facts(self):
        return [F("shoe.storage", "bed", 1, "s1", pred="are stored"), F("shoe.plan", "rack", 5, "s2", mod="proposed", pred="plans to store"),
                F("user.hobby", "chess", 2, "s3", pred="plays"), F("user.hobby", "go", 6, "s4", pred="plays")]

    def test_matches_v4_without_fix_and_protect(self):
        fs = self.facts()
        v4 = [c["pair"] for c in L.candidates(fs, {"theta": 0.12, "per_fact": 5, "max_pairs": 40})]
        v5 = [c["pair"] for c in candidates_v5(fs, dict(BASE, fix=False))]
        self.assertEqual(v4, v5)

    def test_old_new_order_and_same_session_excluded(self):
        fs = self.facts() + [F("shoe.storage", "closet", 3, "s1", pred="are stored")]
        for c in candidates_v5(fs, BASE):
            self.assertNotEqual(c["old"]["session_key"], c["new"]["session_key"])
            self.assertLessEqual(c["old"]["asserted_at"], c["new"]["asserted_at"])

    def test_protect_keeps_each_facts_best_partner_under_the_cap(self):
        fs = self.facts()
        cut = candidates_v5(fs, dict(BASE, max_pairs=1, protect=0))
        self.assertEqual(len(cut), 1)
        prot = candidates_v5(fs, dict(BASE, max_pairs=2, protect=1))
        self.assertEqual(len(prot), 2)
        objs = {tuple(sorted((c["old"]["object"], c["new"]["object"]))) for c in prot}
        self.assertEqual(objs, {("bed", "rack"), ("chess", "go")})


class Verify(unittest.TestCase):
    def test_parse_and_code_side_consistency(self):
        out = ("1: replaces | E1=user / home city; E2=user / home city; same_entity=yes; same_attribute=yes; both_true=no\n"
               "2: replaces | E1=user / office; E2=user / home; same_entity=yes; same_attribute=no; both_true=yes\n"
               "3: pending | E1=shoes / storage; E2=shoes / storage; same_entity=yes; same_attribute=yes; both_true=yes\n"
               "4: unsure | E1=a; E2=b\n"
               "5: replaces | E1=x; E2=x; same_entity=yes; same_attribute=yes; both_true=yes\n")
        got = parse_verify(out, 5)
        self.assertEqual([verified_label(l, f) for l, f, _ in got], ["replaces", "none", "pending", "unsure", "none"])

    def test_missing_fields_never_assert_a_link(self):
        self.assertEqual(verified_label("replaces", {}), "none")
        self.assertEqual(verified_label("pending", {"same_entity": "yes"}), "none")

    def test_missing_pair_raises(self):
        with self.assertRaises(ValueError):
            parse_verify("1: none | x\n", 2)


if __name__ == "__main__":
    unittest.main()
