"""Final-gate finding F2 (reviews/pr/ingest-review-3.md): an undeclared request whose messages are all new after existing history was
classified host_authored with no gap. Written BEFORE the fix; run on 2b23546 to record failing-first.
  test_no_overlap_unseen_user_messages_keep_user_provenance_and_block   behavioural (2 and 3 messages, both providers)
  test_undeclared_leading_unseen_message_before_aligned_run_blocks      behavioural
  test_host_authored_only_when_declared                                 behavioural (undeclared half) + guard (declared half)
  test_partial_overlap_with_new_tail_is_clean                           guard, passes before and after
  test_overlap_not_at_end_of_history_blocks                             guard, passes before and after
  test_first_ever_call_with_several_users_keeps_user_provenance         guard, passes before and after
"""
import json
import os
import shutil
import tempfile
import unittest

from ingest.stubs import text
from ingest.testkit import Rig


def spec(prov, t):
    return {"content": [text(t)]} if prov == "anthropic" else {"message": {"role": "assistant", "content": t}}


def asst(prov, o):
    return {"role": "assistant", "content": o["content"]} if prov == "anthropic" else o["choices"][0]["message"]


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ingd3-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sub(self, n):
        p = os.path.join(self.d, n)
        os.makedirs(p)
        return p

    def after_history(self, prov, name, new_msgs, **kw):
        rig = Rig(self.sub(name), prov, [spec(prov, "old answer"), spec(prov, "new answer")])
        rig.msgs[:] = [{"role": "user", "content": "old question"}]
        o = rig.call(rig.request())
        old_a = asst(prov, o)
        rig.msgs[:] = [m(old_a) if callable(m) else m for m in new_msgs]
        rig.call(rig.request(), **kw)
        rig.sync()
        return rig

    @staticmethod
    def users(rig):
        return sorted(json.loads(e["content"])["text"] for e in rig.events() if e["prov"] == "user")

    @staticmethod
    def hosts(rig):
        return [e for e in rig.events(include_port=True) if e["prov"] == "host"]

    def blocked(self, rig):
        st = rig.stream_id
        self.assertTrue([g for g in rig.ing.gaps(st) if g["blocking"] and g["kind"] == "ambiguous_alignment"])
        self.assertFalse(rig.ing.claim_complete(st, rig.log.head(st)[0]))
        self.assertFalse(rig.ing.claim_exactly_once(st))

    def test_no_overlap_unseen_user_messages_keep_user_provenance_and_block(self):
        for prov in ("anthropic", "openai"):
            for k in (2, 3):
                names = ["first new question", "second new question", "third new question"][:k]
                rig = self.after_history(prov, "%s%d" % (prov, k), [{"role": "user", "content": t} for t in names])
                self.assertEqual(self.users(rig), sorted(names + ["old question"]), (prov, k))
                self.assertEqual(self.hosts(rig), [], (prov, k))
                self.blocked(rig)

    def test_undeclared_leading_unseen_message_before_aligned_run_blocks(self):
        for prov in ("anthropic", "openai"):
            rig = self.after_history(prov, prov, [{"role": "user", "content": "unseen lead"}, {"role": "user", "content": "old question"},
                                                  lambda a: a, {"role": "user", "content": "newest"}])
            self.assertIn("unseen lead", self.users(rig))
            self.assertEqual(self.hosts(rig), [])
            self.blocked(rig)

    def test_host_authored_only_when_declared(self):
        for prov in ("anthropic", "openai"):
            msgs = [{"role": "user", "content": "SUMMARY of earlier chat"}, {"role": "user", "content": "next question"}]
            und = self.after_history(prov, prov + "u", msgs)
            self.assertEqual(self.hosts(und), [])
            self.assertIn("SUMMARY of earlier chat", self.users(und))
            self.blocked(und)
            dec = self.after_history(prov, prov + "d", msgs, new_count=1)
            self.assertEqual([json.loads(e["content"])["text"] for e in self.hosts(dec)], ["SUMMARY of earlier chat"])
            self.assertTrue(dec.ing.claim_exactly_once(dec.stream_id))

    def test_partial_overlap_with_new_tail_is_clean(self):
        for prov in ("anthropic", "openai"):
            for k in (2, 3):
                names = ["n1 question", "n2 question", "n3 question"][:k]
                rig = self.after_history(prov, "%s%d" % (prov, k), [lambda a: a] + [{"role": "user", "content": t} for t in names])
                self.assertEqual(self.users(rig), sorted(names + ["old question"]))
                self.assertTrue(rig.ing.claim_exactly_once(rig.stream_id))

    def test_overlap_not_at_end_of_history_blocks(self):
        for prov in ("anthropic", "openai"):
            rig = self.after_history(prov, prov, [{"role": "user", "content": "old question"}, {"role": "user", "content": "n1"}, {"role": "user", "content": "n2"}])
            self.blocked(rig)

    def test_first_ever_call_with_several_users_keeps_user_provenance(self):
        for prov in ("anthropic", "openai"):
            rig = Rig(self.sub(prov), prov, [spec(prov, "ok")])
            rig.msgs[:] = [{"role": "user", "content": t} for t in ("a first", "b second", "c third")]
            rig.call(rig.request())
            rig.sync()
            self.assertEqual(self.users(rig), ["a first", "b second", "c third"])
            self.assertEqual(self.hosts(rig), [])


if __name__ == "__main__":
    unittest.main()
