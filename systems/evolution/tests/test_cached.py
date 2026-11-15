import os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from memsys import extractor, gen
from evolution import cached_extract as ce


class Cached(unittest.TestCase):
    def test_recipe_postprocessing_equals_direct_extraction(self):
        extractor.extract_orig = getattr(extractor, "extract_orig", extractor.extract)
        sessions = gen.gen_tenant("t", 3, 20, 30)
        n = 0
        for s in sessions:
            for i, e in enumerate(s["events"]):
                ev = dict(tenant_id="t", session_id=s["session_id"], seq=i + 1, event_id=f"t/{s['session_id']}/{i+1}", hash=f"h{i}", ts_event="2026-01-01T00:00:00.000000Z",
                          trust_origin=e["trust_origin"], actor=e["actor"], subject_id=s["subject_id"])
                raw = ce.raw_extract(1, ev, e["content"])
                for recipe in (1, 2):
                    self.assertEqual(ce.to_recipe(recipe, ev, raw), extractor.extract_orig(recipe, ev, e["content"]))
                n += 1
        self.assertGreater(n, 500)

    def test_extractor_version_2_extracts_something_v1_does_not(self):
        extractor.extract_orig = getattr(extractor, "extract_orig", extractor.extract)
        ev = dict(tenant_id="t", session_id="s", seq=1, event_id="t/s/1", hash="h", ts_event="2026-01-01T00:00:00.000000Z", trust_origin="user_typed", actor="user", subject_id="u")
        self.assertEqual(ce.raw_extract(1, ev, "Alice's city was Rome"), [])
        self.assertEqual(len(ce.raw_extract(2, ev, "Alice's city was Rome")), 1)


if __name__ == "__main__":
    unittest.main()
