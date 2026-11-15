import re
import unittest

from phasef.anon import AnonMap, opaque_rid, opaque_sid

# expected values generated from the retrieval owner's eval/emlab/anon.py (feat/eval-scaffold)
THEIRS = {
    "sharegpt_nh3eDLi_9": "s1fffc6f32f0f",
    "sharegpt_oLlUxYj_40": "sd22e05e00ceb",
    "ultrachat_181126": "sa8534afd9523",
    "ultrachat_511593": "s2ce26ff584e5",
    "ultrachat_247189": "s9ac826a952c5",
    "932c7d0d_2": "se6bc5ffbc7f2",
    "answer_c6fd8ebd": "s6312bd8faec1",
    "answer_88841f26_1": "scbae2b89f011",
    "answer_ultrachat_113156": "s6d4c52a9386c",
    "answer_sharegpt_2kpncbX_13": "s122422abbbc9",
    "sharegpt_JXpIEYh_11": "sf0742695a02c",
    "ultrachat_327291": "s1e5e182d2b3d",
    "21ef2d05_2": "sb44a9614a9e5",
    "3ceb6783_2": "s95de7fb496a3",
    "27971ce6_5": "s9c4b7dcdfe4c",
    "3301f749_1": "sbef4d22c5941",
    "af4a2fd1": "sf32445aeef7d",
    "answer_7e9ad7b4_2": "s5a4da2ed7df6",
    "07741c44_1": "se84f093aee89",
    "e1527aa0": "s6f11e9a83d2e"
}


class AnonTests(unittest.TestCase):
    def test_agrees_with_retrieval_owner_on_20_ids(self):
        self.assertEqual(len(THEIRS), 20)
        for sid, exp in THEIRS.items():
            self.assertEqual(opaque_sid(sid), exp)
            self.assertEqual(opaque_rid(sid + ":7"), exp + ":7")

    def test_no_opaque_id_reveals_label(self):
        for sid in THEIRS:
            self.assertIsNone(re.search(r"answer|sharegpt|ultrachat", opaque_sid(sid)))
            self.assertRegex(opaque_sid(sid), r"^s[0-9a-f]{12}$")

    def test_roundtrip_only_through_map(self):
        m = AnonMap(THEIRS)
        sid = "answer_c6fd8ebd"
        self.assertEqual(m.real_rid(opaque_rid(sid + ":3")), sid + ":3")


if __name__ == "__main__":
    unittest.main()
