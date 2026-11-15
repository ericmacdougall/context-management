import unittest
from datetime import datetime

from phasef.supersede_v2 import norm_slot, resolve_v2, slot_match_stats


def F(slot, obj, day, card="single", modality="asserted", author="user", scope="personal", sid=None, order=0):
    return {"slot": slot, "object": obj, "asserted_at": datetime(2023, 1, day), "cardinality": card, "modality": modality,
            "author": author, "scope": scope, "session_id": sid or "s%d" % day, "order": order}


def st(fs):
    return [f["status"] for f in resolve_v2(fs)]


class V2Tests(unittest.TestCase):
    def test_single_slot_newer_replaces_older(self):
        fs = resolve_v2([F("old_sneakers.storage_location", "under the bed", 1), F("sneakers.storage_location", "shoe rack", 5)])
        self.assertEqual([f["status"] for f in fs], ["superseded", "current"])
        self.assertEqual(fs[0]["superseded_by"], 1)

    def test_normaliser_matches_variants_but_not_different_attributes(self):
        self.assertEqual(norm_slot("old_sneakers.storage_location"), norm_slot("sneaker.storage_place"))
        self.assertNotEqual(norm_slot("user.job_title"), norm_slot("user.hobby"))

    def test_multi_cardinality_never_supersedes(self):
        self.assertEqual(st([F("user.purchase", "a", 1, card="multi"), F("user.purchase", "b", 2, card="multi")]), ["current", "current"])

    def test_scope_mismatch_and_modality_gate(self):
        self.assertEqual(st([F("user.job", "a", 1, scope="work"), F("user.job", "b", 2, scope="hobby")]), ["current", "current"])
        self.assertEqual(st([F("user.job", "a", 1), F("user.job", "b", 2, modality="hypothetical")]), ["current", "inactive"])

    def test_assistant_cannot_retire_user_and_duplicate_ignored(self):
        self.assertEqual(st([F("user.pet", "cat", 1), F("user.pet", "dog", 2, author="assistant")]), ["current", "inactive"])
        self.assertEqual(st([F("user.pet", "Cat", 1), F("user.pet", "cat", 2)]), ["current", "duplicate"])

    def test_listed_out_of_order_still_orders_by_date(self):
        self.assertEqual(st([F("user.city", "B", 9), F("user.city", "A", 2)]), ["current", "superseded"])

    def test_slot_match_stats(self):
        fs = resolve_v2([F("user.city", "A", 1), F("user.city", "B", 2), F("user.job", "x", 3)])
        s = slot_match_stats(fs)
        self.assertEqual((s["single_state_facts"], s["in_cross_session_slot"]), (3, 2))


if __name__ == "__main__":
    unittest.main()
