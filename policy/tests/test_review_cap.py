import unittest

from pol.core import MAX_CANDIDATES, genesis
from pol.testkit import run


class Cap(unittest.TestCase):
    def test_candidates_are_bounded(self):
        hs = {"h%d" % i: "human" for i in range(MAX_CANDIDATES + 3)}
        st = genesis("T1", hs, {"r1": list(hs)}, ["h0"])
        evs = [{"type": "propose", "id": "p%d" % i, "idem": "p%d" % i, "req": "r1", "principal": "h0", "value": "v%d" % i, "based_on": 0} for i in range(MAX_CANDIDATES + 3)]
        for i in range(MAX_CANDIDATES + 3):
            evs.append({"type": "decide", "id": "d%d" % i, "idem": "d%d" % i, "req": "r1", "principal": "h%d" % i, "verb": "accept", "proposal": "p%d" % i, "channel": "owner_ui", "based_on": 0})
        st, codes = run(st, evs)
        self.assertIn("candidates_full", codes)
        self.assertLessEqual(len(st["reqs"]["r1"]["contested"]["candidates"]), MAX_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
