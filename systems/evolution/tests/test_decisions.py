import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from evolution import decisions as D


def ev(seq, kind, body, trust="user_typed"):
    return {"seq": seq, "kind": kind, "trust_origin": trust, "body": body}


DEF = ev(1, "task_defined", {"task_id": "T", "items": [{"id": "a", "type": "scope", "text": "EU only"}, {"id": "b", "type": "goal", "text": "ship"}]})
DET = lambda seq, cid, text, item="a": ev(seq, "change_detected", {"change_id": cid, "item": item, "op": "change", "proposed_text": text, "quote": "q", "quote_event_id": "e", "speaker": "x", "speaker_trust": "assistant", "detector": "d", "confidence": .9}, "tool")


class Decisions(unittest.TestCase):
    def test_detected_change_does_not_alter_definition(self):
        s = D.fold([DEF, DET(2, "c1", "US only")])
        self.assertEqual(s["items"]["a"]["text"], "EU only"); self.assertEqual(D.item_status(s)["a"], "pending_change")

    def test_only_user_typed_decides(self):
        for t in ("assistant", "web", "user_supplied_external"):
            s = D.fold([DEF, DET(2, "c1", "US only"), ev(3, "decision_recorded", {"decision_id": "d1", "action": "accept", "change_id": "c1"}, t)])
            self.assertEqual(s["items"]["a"]["text"], "EU only"); self.assertEqual(len(s["unauthorized"]), 1)
        s = D.fold([DEF, DET(2, "c1", "US only"), ev(3, "decision_recorded", {"decision_id": "d1", "action": "accept", "change_id": "c1"})])
        self.assertEqual(s["items"]["a"]["text"], "US only")
        self.assertEqual(D.view(s)["item_chain"]["a"][-1]["source_seq"], 2)

    def test_conflicting_pending_is_disputed_and_reject_keeps_history(self):
        s = D.fold([DEF, DET(2, "c1", "US only"), DET(3, "c2", "APAC only"), ev(4, "decision_recorded", {"decision_id": "d1", "action": "reject", "change_id": "c1"})])
        self.assertEqual(D.item_status(D.fold([DEF, DET(2, "c1", "US only"), DET(3, "c2", "APAC only")]))["a"], "disputed")
        self.assertEqual(D.item_status(s)["a"], "pending_change"); self.assertEqual(D.view(s)["history"]["c1"]["disposition"], "rejected")

    def test_revert_restores_state_exactly_and_stale_work(self):
        base = [DEF, DET(2, "c1", "US only"), ev(3, "step_completed", {"step_id": "s", "serves": ["a"], "artifacts": [], "def_version": 1, "watermark": 2}, "assistant")]
        s0 = D.fold(base)
        acc = ev(4, "decision_recorded", {"decision_id": "d1", "action": "accept", "change_id": "c1"})
        s1 = D.fold(base + [acc])
        self.assertEqual([x[1] for x in D.stale_work(s1)], ["s"])
        s2 = D.fold(base + [acc, ev(5, "decision_recorded", {"decision_id": "d2", "action": "revert", "reverts": "d1"})])
        self.assertEqual(D.core(s0), D.core(s2)); self.assertEqual(D.stale_work(s2), [])

    def test_broken_variant_detected_applies_is_caught(self):
        s = D.fold([DEF, DET(2, "c1", "US only")], broken="detected_applies")
        self.assertEqual(s["items"]["a"]["text"], "US only")


if __name__ == "__main__":
    unittest.main()
