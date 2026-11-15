import unittest
from datetime import datetime

from phasef.facts import parse_extraction, score_facts, _json_from

BATCH = [("s1", "2023/05/20 (Sat) 02:21", []), ("s2", "2023/06/01 (Thu) 10:00", [])]
GOOD = ('{"sessions": [{"session_id": "s1", "facts": [{"subject": "user", "predicate": "owns", "object": "a cat", '
        '"valid_from": null, "valid_to": null, "modality": "asserted", "scope": "personal", "author": "user", "round": 2, '
        '"cue_keys": ["pet", "cat"]}]}, {"session_id": "s2", "facts": []}]}')


class FactsTests(unittest.TestCase):
    def test_parse_defaults_valid_from_to_session_date(self):
        per, dropped = parse_extraction(GOOD, BATCH)
        f = per[0][0]
        self.assertEqual(f["valid_from"], datetime(2023, 5, 20, 2, 21))
        self.assertEqual((f["round"], f["author"], dropped), (2, "user", 0))

    def test_truncated_closers_and_tag_junk_are_repaired(self):
        broken = GOOD[:-2] + "</parameter>\n</invoke>"  # drops the final "]}" like the real batch 16
        self.assertEqual(_json_from(broken)["sessions"][0]["session_id"], "s1")
        per, _ = parse_extraction(broken, BATCH)
        self.assertEqual(len(per[0]), 1)

    def test_same_session_id_twice_in_a_batch_keeps_dates_apart(self):
        batch = [("x", "2023/05/01 (Mon) 10:00", []), ("x", "2023/08/01 (Tue) 10:00", [])]
        mk = lambda o: ('{"session_id": "x", "facts": [{"subject": "u", "predicate": "p", "object": "%s"}]}' % o)
        per, _ = parse_extraction('{"sessions": [%s, %s]}' % (mk("first"), mk("second")), batch)
        self.assertEqual([(f[0]["object"], f[0]["asserted_at"].month) for f in per], [("first", 5), ("second", 8)])

    def test_unknown_session_and_missing_fields_are_dropped_and_counted(self):
        bad = '{"sessions": [{"session_id": "zzz", "facts": []}, {"session_id": "s1", "facts": [{"subject": "x"}]}]}'
        per, dropped = parse_extraction(bad, BATCH)
        self.assertEqual((dropped, per[0]), (2, []))

    def test_unknown_modality_falls_back_and_cue_keys_capped(self):
        t = GOOD.replace('"asserted"', '"wish"').replace('["pet", "cat"]', '["a","b","c","d","e","f","g"]')
        f = parse_extraction(t, BATCH)[0][0][0]
        self.assertEqual((f["modality"], len(f["cue_keys"])), ("asserted", 6))

    def test_cue_keys_find_fact_with_no_word_overlap(self):
        mk = lambda o, cues: {"subject": "user", "predicate": "keeps", "object": o, "cue_keys": cues, "asserted_at": datetime(2023, 1, 1)}
        facts = [mk("Adidas Superstars under the bed", ["sneakers", "shoes storage"]), mk("a blue mug on the desk", ["mug"])]
        hit = score_facts("where are my shoes stored", facts)
        self.assertEqual(hit[0]["object"], "Adidas Superstars under the bed")
        facts[0]["cue_keys"] = []
        self.assertEqual(score_facts("where are my shoes stored", facts), [])


class AnonParse(unittest.TestCase):
    def test_v2_parse_matches_opaque_echo_and_keeps_real_id(self):
        from phasef.anon import opaque_sid
        from phasef.facts_v2 import build_extract_prompt_v2, parse_extraction_v2
        batch = [("answer_abc123", "2023/05/20 (Sat) 02:21", [{"role": "user", "content": "hi"}])]
        prompt = build_extract_prompt_v2(batch)
        self.assertNotIn("answer_abc123", prompt)
        self.assertIn(opaque_sid("answer_abc123"), prompt)
        out = '{"sessions": [{"session_id": "%s", "facts": [{"subject": "u", "slot": "u.x", "predicate": "p", "object": "o"}]}]}' % opaque_sid("answer_abc123")
        per, dropped = parse_extraction_v2(out, batch)
        self.assertEqual((dropped, per[0][0]["session_id"]), (0, "answer_abc123"))


if __name__ == "__main__":
    unittest.main()
