import os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from evolution import upcast


class Upcast(unittest.TestCase):
    def test_v1_and_v2_converge(self):
        rng = random.Random(3)
        for _ in range(500):
            logical = {"step_id": f"s{rng.randint(0,99)}", "artifacts": [f"a{k}" for k in range(rng.randint(0, 4))], "outcome": "ok"}
            v1 = {k: x for k, x in logical.items() if k != "outcome"}
            self.assertEqual(upcast.upcast("step_completed", upcast.encode_v2(logical)), logical)
            self.assertEqual(upcast.upcast("step_completed", v1), {**logical, "outcome": "unknown"})

    def test_v2_requires_outcome_and_unknown_version_rejected(self):
        with self.assertRaises(ValueError): upcast.upcast("step_completed", {"_v": 2, "step_id": "s", "produced": []})
        with self.assertRaises(ValueError): upcast.upcast("step_completed", {"_v": 9, "step_id": "s"})

    def test_other_kinds_untouched(self):
        self.assertEqual(upcast.upcast("step_started", {"step_id": "s"}), {"step_id": "s"})


if __name__ == "__main__":
    unittest.main()
