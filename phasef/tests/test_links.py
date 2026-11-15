import unittest
from datetime import datetime

from phasef.links import candidates, derive_status, fid, parse_labels


def F(slot, obj, day, sid, mod="asserted", card="single", author="user", pred="p", cues=()):
    return {"slot": slot, "predicate": pred, "subject": "user", "object": obj, "asserted_at": datetime(2023, 1, day), "modality": mod, "cardinality": card,
            "author": author, "scope": "personal", "session_id": sid, "session_key": sid + "k", "round": 0, "order": day, "quote": "q", "cue_keys": list(cues)}


class Links(unittest.TestCase):
    def test_ids_are_content_derived_and_stable(self):
        a = F("x.y", "1", 1, "s1")
        self.assertEqual(fid(a), fid(dict(a, order=99)))
        self.assertNotEqual(fid(a), fid(dict(a, object="2")))

    def test_candidates_pair_across_sessions_only_and_respect_caps(self):
        facts = [F("shoes.storage_location", "bed", 1, "s1", cues=["shoe storage"]), F("shoe.plan", "rack", 5, "s2", mod="decided", cues=["shoe storage"]),
                 F("shoes.storage_location", "closet", 3, "s1", cues=["shoe storage"]), F("user.hobby_x", "z", 2, "s3", cues=["unrelated words"])]
        c = candidates(facts, {"theta": 0.2, "per_fact": 3, "max_pairs": 30})
        pairs = {tuple(sorted((p["old"]["object"], p["new"]["object"]))) for p in c}
        self.assertIn(("bed", "rack"), pairs)
        self.assertNotIn(("bed", "closet"), pairs)  # same session
        self.assertEqual(len(candidates(facts, {"theta": 0.2, "per_fact": 3, "max_pairs": 1})), 1)
        multi = [dict(f, cardinality="multi") for f in facts]
        self.assertEqual(len(candidates(multi)), len(candidates(facts)))  # cardinality is not a filter (extractor mislabels counts)
        hypo = [dict(f, modality="hypothetical") for f in facts]
        self.assertEqual(candidates(hypo), [])

    def test_status_from_decisions_orders_by_date_and_is_advisory(self):
        o, n = F("a", "old", 1, "s1"), F("a", "new", 5, "s2")
        fs, links = derive_status([n, o], {tuple(sorted((fid(o), fid(n)))): "replaces"})
        self.assertEqual([f["status"] for f in fs], ["current", "superseded"])
        self.assertEqual(len(fs), 2)
        self.assertEqual(links[0][0], "replaces")

    def test_pending_does_not_retire_and_restates_does_nothing(self):
        o, n = F("a", "old", 1, "s1"), F("a", "plan", 5, "s2", mod="decided")
        fs, _ = derive_status([o, n], {tuple(sorted((fid(o), fid(n)))): "pending"})
        self.assertEqual([f["status"] for f in fs], ["current", "pending-update"])
        fs, links = derive_status([o, n], {tuple(sorted((fid(o), fid(n)))): "restates"})
        self.assertEqual(([f["status"] for f in fs], links), (["current", "current"], []))

    def test_lower_trust_replacing_higher_is_disputed(self):
        o, n = F("a", "old", 1, "s1"), F("a", "new", 5, "s2", author="assistant")
        fs, _ = derive_status([o, n], {tuple(sorted((fid(o), fid(n)))): "replaces"})
        self.assertEqual([f["status"] for f in fs], ["current", "disputed"])

    def test_parse_labels(self):
        t = "1: replaces | new value\n2. restates | same\n3: adds"
        self.assertEqual(parse_labels(t, 3), ["replaces", "restates", "adds"])
        with self.assertRaises(ValueError):
            parse_labels("1: replaces | x", 2)


if __name__ == "__main__":
    unittest.main()
