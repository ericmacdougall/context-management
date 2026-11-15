import unittest
from datetime import datetime

from phasef.supersede import resolve, same_slot


def F(subj, pred, obj, day, modality="asserted", author="user", order=0, sid="s"):
    d = datetime(2023, 1, day)
    return {"subject": subj, "predicate": pred, "object": obj, "valid_from": d, "asserted_at": d,
            "modality": modality, "author": author, "session_id": sid, "round": 0, "order": order}


class SupersedeTests(unittest.TestCase):
    def test_later_value_is_current_even_if_listed_first(self):
        fs = resolve([F("user", "lives in", "Boston", 20), F("user", "lives in", "Denver", 5)])
        self.assertEqual(fs[0]["status"], "current")
        self.assertEqual(fs[1]["status"], "superseded")
        self.assertEqual(fs[1]["valid_to"], datetime(2023, 1, 20))
        self.assertEqual(fs[1]["superseded_by"], 0)

    def test_non_state_modalities_never_supersede_or_become_current(self):
        fs = resolve([F("user", "lives in", "Denver", 5), F("user", "lives in", "Paris", 9, modality="hypothetical"),
                      F("user", "lives in", "Rome", 10, modality="question"),
                      F("user", "lives in", "Oslo", 11, modality="proposed")])
        self.assertEqual([f["status"] for f in fs], ["current", "inactive", "inactive", "inactive"])

    def test_assistant_cannot_retire_user_fact(self):
        fs = resolve([F("user", "owns", "a cat", 5), F("user", "owns", "a dog", 9, author="assistant")])
        self.assertEqual([f["status"] for f in fs], ["current", "inactive"])

    def test_user_can_retire_assistant_fact(self):
        fs = resolve([F("user", "owns", "a cat", 5, author="assistant"), F("user", "owns", "a dog", 9)])
        self.assertEqual([f["status"] for f in fs], ["superseded", "current"])

    def test_same_object_restated_does_not_supersede(self):
        fs = resolve([F("user", "lives in", "Denver", 5), F("user", "lives in", "denver", 9)])
        self.assertEqual([f["status"] for f in fs], ["current", "duplicate"])

    def test_back_and_forth(self):
        fs = resolve([F("user", "lives in", "A", 1), F("user", "lives in", "B", 2), F("user", "lives in", "A", 3)])
        self.assertEqual([f["status"] for f in fs], ["superseded", "superseded", "current"])

    def test_different_slots_do_not_interact(self):
        fs = resolve([F("user", "lives in", "Denver", 5), F("user", "works at", "Acme", 9),
                      F("sister", "lives in", "Rome", 9)])
        self.assertEqual([f["status"] for f in fs], ["current"] * 3)

    def test_predicate_paraphrase_joins_slot(self):
        a, b = F("user", "current job title", "analyst", 1), F("user", "job title", "manager", 5)
        self.assertTrue(same_slot(a, b))
        fs = resolve([a, b])
        self.assertEqual([f["status"] for f in fs], ["superseded", "current"])
        self.assertFalse(same_slot(F("user", "job title", "x", 1), F("user", "hobby", "x", 1)))

    def test_tie_on_dates_broken_by_extraction_order(self):
        fs = resolve([F("user", "pb", "20 min", 5, order=1), F("user", "pb", "18 min", 5, order=2)])
        self.assertEqual([f["status"] for f in fs], ["superseded", "current"])


if __name__ == "__main__":
    unittest.main()
