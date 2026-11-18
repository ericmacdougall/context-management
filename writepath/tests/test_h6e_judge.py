"""Offline: the D6 verdict applies amendment H6e (review J M7) to a run's record (exp/h_d6_broker.py judge, PROBE_FILES).
  J1  a fenced record where every case holds gives no reason and no INVALID-REQUEST case
  J2  C14a: B's item of the mixed bulk accepted -> a reason
  J3  C14a: A's own item refused -> INVALID-REQUEST (the B refusal would be vacuous)
  J4  C14b: a reindex from B's read alias that created documents -> a reason
  J5  C14c: a write through the stale name without require_alias accepted -> a reason
  J6  C14d: a mapping update through B's write alias accepted -> a reason; through A's own alias -> no reason (reported)
  J7  a record without C14 -> "C14 missing"
  J8  C9 probes the three role-key files and seal_key; any of them readable -> a reason
usage (from writepath/): python3 -m unittest tests.test_h6e_judge -v"""
import copy
import importlib
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
H = importlib.import_module("exp.h_d6_broker")
NEW_FILES = ("projector_role_key", "eraser_role_key", "verifier_role_key", "seal_key")
REF = [403, "security_exception"]


def holding():
    files = {n: "EACCES" for n in H.PROBE_FILES}
    for n in NEW_FILES:
        files.setdefault(n, "EACCES")
    return {
        "own_write": [201, None], "files": files, "invalid": {},
        "C1": {"B_w": REF, "B_read": REF, "B_x": REF}, "C2": {"A_base": REF}, "C3": {"A_x": REF},
        "C4": {"add_next": REF, "remove_own": REF, "add_on_B": REF},
        "C14": {"a": {"request": 200, "own": [201, None], "B": REF}, "b": [403, "security_exception", None],
                "c": {"writes": [REF] * 5, "accepted": 0}, "d": {"own_mapping": [200, None], "B_mapping": REF}},
        "C5": {"E2": 3, "h2_write": [201, None], "stale": [REF] * 20, "stale_accepted": 0},
        "C6": {"old_epoch_h1": "Refused: x", "new_epoch_h1": "Refused: x", "new_epoch_empty": "Refused: x"},
        "C7": {"p1_step_after_takeover": -1, "p1_written_before": 3, "p1_written_after": 3, "p2_done": True},
        "C7_events_in_read_alias": 5, "C8": {"n_errors": 0}, "C8_descriptors": {"n_bad": 0, "checked": 2 * H.N_PAIRS},
        "C10": {"holder_fn": "callable", "select_leases": "42501"}, "C11": "peer_refused",
        "C12": {"b_projector_start": "failed", "c_stale_accepted": 0, "a_window_write": [201, None]},
        "C13": {"values_found": 0, "key_hashes": 3},
    }


WIT = {n: "yes" for n in list(H.PROBE_FILES) + list(NEW_FILES)}


class H6eJudge(unittest.TestCase):
    def judge(self, x):
        return H.judge("fenced", x, WIT)

    def test_j1_holding(self):
        self.assertEqual(self.judge(holding()), ([], {}))

    def test_j2_mixed_bulk_B_item(self):
        x = holding()
        x["C14"]["a"]["B"] = [201, None]
        self.assertTrue(any(w.startswith("C14a") for w in self.judge(x)[0]), "B's bulk item accepted, no reason given")

    def test_j3_mixed_bulk_own_refused_is_invalid(self):
        x = holding()
        x["C14"]["a"]["own"] = REF
        self.assertIn("C14a own", self.judge(x)[1])

    def test_j4_reindex_created(self):
        x = holding()
        x["C14"]["b"] = [200, None, 4]
        self.assertTrue(any(w.startswith("C14b") for w in self.judge(x)[0]))

    def test_j5_stale_without_require_alias(self):
        x = holding()
        x["C14"]["c"]["accepted"] = 1
        self.assertTrue(any(w.startswith("C14c") for w in self.judge(x)[0]))

    def test_j6_mapping(self):
        x = holding()
        x["C14"]["d"]["B_mapping"] = [200, None]
        self.assertTrue(any(w.startswith("C14d") for w in self.judge(x)[0]))
        x = holding()
        x["C14"]["d"]["own_mapping"] = [200, None]
        self.assertEqual(self.judge(x), ([], {}))

    def test_j7_missing(self):
        x = holding()
        del x["C14"]
        self.assertIn("C14 missing", self.judge(x)[0])

    def test_j8_c9_files(self):
        for n in NEW_FILES:
            self.assertIn(n, H.PROBE_FILES, f"C9 does not probe {n}")
        x = copy.deepcopy(holding())
        x["files"]["seal_key"] = "readable"
        self.assertIn("C9 seal_key: readable", self.judge(x)[0])


if __name__ == "__main__":
    unittest.main()
