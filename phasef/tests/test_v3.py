import unittest
from datetime import datetime

from phasef.canon import parse_mapping
from phasef.supersede_v3 import resolve_v3, slot_rate


def F(slot, obj, day, mod="asserted", card="single", author="user", sid=None, order=0):
    return {"slot": slot, "object": obj, "asserted_at": datetime(2023, 1, day), "modality": mod, "cardinality": card,
            "author": author, "scope": "personal", "session_id": sid or "s%d" % day, "order": order, "quote": "q"}


class V3(unittest.TestCase):
    def test_mapping_must_be_pure(self):
        keys = ["a.b", "c.d"]
        self.assertEqual(parse_mapping('{"mapping": {"a.b": "a.b", "c.d": "a.b"}}', keys)["c.d"], "a.b")
        for bad in ('{"mapping": {"a.b": "a.b"}}', '{"mapping": {"a.b": "a.b", "c.d": "x.y"}}',
                    '{"mapping": {"a.b": "a.b", "c.d": "c.d", "e.f": "e.f"}}', '{"nope": 1}'):
            with self.assertRaises(ValueError):
                parse_mapping(bad, keys)

    def test_plan_in_other_slot_links_through_mapping_and_retires_nothing(self):
        fs = [F("shoes.storage_location", "under the bed", 1), F("user.shoe_storage_plan", "shoe rack", 5, mod="proposed")]
        out, links = resolve_v3(fs, {"user.shoe_storage_plan": "shoes.storage_location"})
        self.assertEqual([f["status"] for f in out], ["current", "pending-update"])
        self.assertEqual(links, [("pending", 0, 1)])
        out, links = resolve_v3([F("shoes.storage_location", "under the bed", 1), F("user.shoe_storage_plan", "shoe rack", 5, mod="proposed")])
        self.assertEqual(links, [])  # no mapping: no link (the v2 failure)

    def test_decided_is_pending_not_superseding_asserted_is_superseding(self):
        out, links = resolve_v3([F("user.city", "A", 1), F("user.city", "B", 2, mod="decided")])
        self.assertEqual([f["status"] for f in out], ["current", "pending-update"])
        out, links = resolve_v3([F("user.city", "A", 1), F("user.city", "B", 2)])
        self.assertEqual([f["status"] for f in out], ["superseded", "current"])
        self.assertEqual(links, [("supersession", 0, 1)])

    def test_multi_never_linked_and_trust_gate_and_same_object(self):
        out, links = resolve_v3([F("user.buy", "a", 1, card="multi"), F("user.buy", "b", 2, card="multi")])
        self.assertEqual(links, [])
        out, links = resolve_v3([F("user.pet", "cat", 1), F("user.pet", "dog", 2, author="assistant")])
        self.assertEqual([f["status"] for f in out], ["current", "inactive"])
        out, links = resolve_v3([F("user.pet", "Cat", 1), F("user.pet", "cat", 2)])
        self.assertEqual(links, [])

    def test_disputed_same_session_and_nothing_is_dropped(self):
        out, _ = resolve_v3([F("user.pet", "cat", 3, sid="x"), F("user.pet", "dog", 3, sid="x", order=1)])
        self.assertEqual([f["status"] for f in out], ["disputed", "disputed"])
        self.assertEqual(len(out), 2)

    def test_slot_rate_before_after(self):
        fs, _ = resolve_v3([F("a.x", "1", 1), F("b.y", "2", 2)], {"b.y": "a.x"})
        self.assertEqual(slot_rate(fs, "canon")["hit"], 2)
        self.assertEqual(slot_rate(fs, "slot")["hit"], 0)


if __name__ == "__main__":
    unittest.main()
