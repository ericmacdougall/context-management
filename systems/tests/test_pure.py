import itertools
import random
import unittest

from memsys import chain, extractor, fencing, fusion, rules
from memsys.rules import Claim, resolve_slot, TRUST_RANK

BROKEN_RANK = {k: 1 for k in TRUST_RANK}  # every origin equal: no trust gating


def C(i, obj, trust="user_typed", modality="asserted", t=0):
    return Claim(id=f"c{i}", object=obj, modality=modality, trust_origin=trust,
                 valid_from=f"2026-01-{t + 1:02d}T00:00:00.000000Z",
                 order_key=f"2026-01-{t + 1:02d}T00:00:00.000000Z|s|{i:012d}|000")


def violations(claims, res):
    """Invariants any correct resolution must satisfy."""
    by = {c.id: c for c in claims}
    v = []
    cur = [i for i, (s, _) in res.items() if s == "current"]
    if len([c for c in claims if c.modality in ("asserted", "decided")]) and len(cur) != 1:
        v.append("not exactly one current")
    for i, (s, vt) in res.items():
        c = by[i]
        if c.modality in ("hypothetical", "proposed") and s == "current":
            v.append("non-assertive became current")
        if s == "superseded":
            # whoever closed it must be at least as trusted
            closer = [x for x in claims if x.valid_from == vt and res[x.id][0] == "current"]
            for x in closer:
                if TRUST_RANK[x.trust_origin] < TRUST_RANK[c.trust_origin]:
                    v.append("lower trust closed higher")
    # a user_typed claim, once current, can only be closed by user_typed
    return v


class Supersession(unittest.TestCase):
    def test_web_cannot_close_user_typed(self):
        cs = [C(1, "blue", "user_typed", t=0), C(2, "red", "web", t=1)]
        r = resolve_slot(cs)
        self.assertEqual(r["c1"][0], "current")
        self.assertEqual(r["c2"][0], "disputed")

    def test_pasted_external_cannot_close_user_typed(self):
        cs = [C(1, "blue", "user_typed", t=0), C(2, "red", "user_supplied_external", t=1)]
        self.assertEqual(resolve_slot(cs)["c2"][0], "disputed")

    def test_assistant_restatement_cannot_supersede_user(self):
        cs = [C(1, "blue", "user_typed", t=0), C(2, "red", "user_typed", t=1), C(3, "blue", "assistant", t=2)]
        r = resolve_slot(cs)
        self.assertEqual(r["c2"][0], "current")
        self.assertEqual(r["c3"][0], "disputed")
        self.assertEqual(r["c1"][0], "superseded")

    def test_hypothetical_and_proposed_do_not_supersede(self):
        cs = [C(1, "blue", t=0), C(2, "red", modality="hypothetical", t=1), C(3, "green", modality="proposed", t=2)]
        r = resolve_slot(cs)
        self.assertEqual(r["c1"][0], "current")
        self.assertEqual(r["c2"][0], "hypothetical")
        self.assertEqual(r["c3"][0], "proposed")

    def test_user_typed_supersedes_user_typed_and_closes_interval(self):
        cs = [C(1, "blue", t=0), C(2, "red", t=3)]
        r = resolve_slot(cs)
        self.assertEqual(r["c1"], ("superseded", cs[1].valid_from))
        self.assertEqual(r["c2"], ("current", None))

    def test_higher_trust_supersedes_lower(self):
        cs = [C(1, "blue", "web", t=0), C(2, "red", "user_typed", t=1)]
        self.assertEqual(resolve_slot(cs)["c1"][0], "superseded")

    def test_same_value_lower_trust_is_duplicate(self):
        cs = [C(1, "Blue", t=0), C(2, "blue", "assistant", t=1)]
        self.assertEqual(resolve_slot(cs)["c2"][0], "duplicate")

    def test_order_independence_all_permutations(self):
        cs = [C(1, "a", "web", t=0), C(2, "b", "user_typed", t=1), C(3, "c", "assistant", t=2),
              C(4, "d", "user_typed", t=3), C(5, "e", modality="hypothetical", t=4)]
        want = resolve_slot(cs)
        for p in itertools.permutations(cs):
            self.assertEqual(resolve_slot(list(p)), want)

    def test_erasing_superseder_reinstates_predecessor(self):
        cs = [C(1, "blue", t=0), C(2, "red", t=1)]
        self.assertEqual(resolve_slot(cs[:1])["c1"], ("current", None))
        self.assertEqual(resolve_slot(cs)["c1"][0], "superseded")

    def test_property_random_cases(self):
        rng = random.Random(16096)
        origins = list(TRUST_RANK)
        mods = ["asserted", "asserted", "asserted", "decided", "hypothetical", "proposed"]
        n = 0
        for _ in range(3000):
            k = rng.randint(1, 8)
            cs = [C(i, rng.choice("abc"), rng.choice(origins), rng.choice(mods), t=rng.randint(0, 20)) for i in range(k)]
            res = resolve_slot(cs)
            self.assertEqual(violations(cs, res), [], cs)
            n += 1
            # user_typed current is never displaced by lower trust
            for i, c in enumerate(cs):
                if res[c.id][0] == "superseded":
                    closer = [x for x in cs if x.valid_from == res[c.id][1] and res[x.id][0] in ("current", "superseded")]
                    self.assertTrue(any(TRUST_RANK[x.trust_origin] >= TRUST_RANK[c.trust_origin] for x in closer))
        self.assertEqual(n, 3000)

    def test_broken_variant_is_caught(self):
        """With trust gating removed the invariant check must report violations."""
        cs = [C(1, "blue", "user_typed", t=0), C(2, "red", "web", t=1)]
        res = resolve_slot(cs, rank=BROKEN_RANK)
        self.assertEqual(res["c1"][0], "superseded")  # broken behaviour
        self.assertIn("lower trust closed higher", violations(cs, res))


class ScopeAndMarkers(unittest.TestCase):
    def sc(self, i, obj, trust, scope, t):
        c = C(i, obj, trust, t=t); c.scope = scope; return c

    def test_lower_trust_scoped_claim_cannot_sit_beside_higher_trust_whole_claim(self):
        cs = [self.sc(1, "555-1", "user_typed", "*", 0), self.sc(2, "666-2", "web", "work", 1)]
        r = resolve_slot(cs)
        self.assertEqual(r["c1"][0], "current"); self.assertEqual(r["c2"][0], "disputed")
        # the broken variant (scope bypasses trust gating) leaves both current
        rb = resolve_slot(cs, cross_scope=False)
        self.assertEqual((rb["c1"][0], rb["c2"][0]), ("current", "current"))

    def test_equal_or_higher_trust_scoped_claim_is_fine_and_scoped_does_not_close_whole(self):
        cs = [self.sc(1, "555-1", "user_typed", "*", 0), self.sc(2, "666-2", "user_typed", "work", 1)]
        r = resolve_slot(cs)
        self.assertEqual((r["c1"][0], r["c2"][0]), ("current", "current"))
        cs = [self.sc(1, "555-1", "user_typed", "*", 0), self.sc(2, "555-1", "web", "work", 1)]     # same value: no conflict
        self.assertNotEqual(resolve_slot(cs)["c2"][0], "disputed")

    def test_paste_and_quote_markers_lower_trust_of_a_user_typed_event(self):
        ev = dict(TestEV, trust_origin="user_typed", actor="user")
        self.assertEqual(extractor.extract(1, ev, "Pasted: Alice's city is Rome")[0]["trust_origin"], "user_supplied_external")
        self.assertEqual(extractor.extract(1, ev, "As you said, Alice's city is Rome")[0]["trust_origin"], "assistant")
        self.assertEqual(extractor.extract(1, ev, "According to the page, Alice's city is Rome")[0]["trust_origin"], "web")
        self.assertEqual(extractor.extract(1, ev, "Alice's city is Rome")[0]["trust_origin"], "user_typed")
        # a marker never RAISES trust
        self.assertEqual(extractor.extract(1, dict(ev, trust_origin="web", actor="tool"), "Pasted: Alice's city is Rome")[0]["trust_origin"], "web")


TestEV = dict(tenant_id="t01", session_id="s1", seq=1, event_id="t01/s1/1", hash="h", ts_event="2026-01-01T00:00:00.000000Z", subject_id="u1")


class Extractor(unittest.TestCase):
    EV = dict(tenant_id="t01", session_id="s1", seq=1, event_id="t01/s1/1", hash="h", ts_event="2026-01-01T00:00:00.000000Z",
              trust_origin="user_typed", actor="user", subject_id="u1")

    def test_assertion_scope_hypo_proposal_rename_chatter(self):
        x = lambda s, **kw: extractor.extract(1, {**self.EV, **kw}, s)
        a = x("Alice's favourite colour is blue.")[0]
        self.assertEqual((a["subject"], a["predicate"], a["object"], a["scope"], a["modality"]), ("Alice", "favourite colour", "blue", "*", "asserted"))
        self.assertEqual(x("Alice's phone for work is 555-1")[0]["scope"], "work")
        self.assertEqual(x("If Alice's city were Rome")[0]["modality"], "hypothetical")
        self.assertEqual(x("Proposal: set Alice's city to Rome")[0]["modality"], "proposed")
        self.assertEqual(x("Alice is now called Alicia")[0]["predicate"], "renamed_to")
        self.assertEqual(x("hello there how are you"), [])
        self.assertEqual(x("Decided: Alice's city is Oslo")[0]["modality"], "decided")

    def test_trust_comes_from_envelope_not_text(self):
        c = extractor.extract(1, {**self.EV, "trust_origin": "web"}, "As you said, Alice's city is Oslo")[0]
        self.assertEqual(c["trust_origin"], "web")

    def test_recipe_versions_differ_and_ids_are_stable(self):
        a1 = extractor.extract(1, self.EV, "Alice's favourite colour is blue")[0]
        a2 = extractor.extract(2, self.EV, "Alice's favourite colour is blue")[0]
        self.assertEqual(a2["predicate"], "favorite color")
        self.assertNotEqual(a1["id"], a2["id"])
        self.assertEqual(a1["id"], extractor.extract(1, self.EV, "Alice's favourite colour is blue")[0]["id"])
        self.assertNotIn("blue", a1["id"])


class Chain(unittest.TestCase):
    def build(self, n=5):
        rows, prev = [], chain.GENESIS
        for i in range(1, n + 1):
            r = dict(tenant_id="t", session_id="s", seq=i, event_id=f"t/s/{i}", ts_event="x", actor="u",
                     trust_origin="user_typed", kind="m", subject_id="u", content_enc=bytes([i]) * 20, prev_hash=prev)
            r["hash"] = chain.event_hash(prev, r["tenant_id"], r["session_id"], r["seq"], r["event_id"], r["ts_event"],
                                         r["actor"], r["trust_origin"], r["kind"], r["subject_id"], r["content_enc"])
            prev = r["hash"]
            rows.append(r)
        return rows

    def test_valid(self):
        self.assertEqual(chain.verify_chain(self.build()), (True, None, 5))

    def test_tamper_gap_reorder_detected(self):
        rows = self.build()
        rows[2]["content_enc"] = b"x" * 20
        self.assertEqual(chain.verify_chain(rows)[:2], (False, 3))
        rows = self.build(); del rows[2]
        self.assertFalse(chain.verify_chain(rows)[0])
        rows = self.build(); rows[1], rows[2] = rows[2], rows[1]
        self.assertFalse(chain.verify_chain(rows)[0])
        rows = self.build(); rows[3]["prev_hash"] = "0" * 64
        self.assertFalse(chain.verify_chain(rows)[0])

    def test_field_boundary_ambiguity(self):
        a = chain.event_hash("p", "ab", "c", 1, "e", "t", "a", "o", "k", "s", b"x")
        b = chain.event_hash("p", "a", "bc", 1, "e", "t", "a", "o", "k", "s", b"x")
        self.assertNotEqual(a, b)


class Fusion(unittest.TestCase):
    def test_rrf(self):
        out = fusion.rrf([["a", "b", "c"], ["c", "b", "d"]], topn=3)
        self.assertEqual([i for i, _ in out], ["c", "b", "a"])

    def test_rrf_dedup_within_list_and_tiebreak(self):
        out = fusion.rrf([["a", "a", "b"], ["b"]], topn=2)
        self.assertEqual(out[0][0], "b")

    def test_rrf_not_just_first_list(self):
        self.assertNotEqual(fusion.rrf([["a"], ["b", "a"]], topn=1)[0][0], "b")

    def test_embed_deterministic_unit(self):
        v = fusion.embed("hello world")
        self.assertEqual(v, fusion.embed("hello world"))
        self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=3)
        self.assertGreater(fusion.cosine(v, fusion.embed("hello there world")), fusion.cosine(v, fusion.embed("zzz qqq")))


class Fencing(unittest.TestCase):
    def test_commit_allowed(self):
        self.assertTrue(fencing.commit_allowed(["e1"], {"e2"}))
        self.assertFalse(fencing.commit_allowed(["e1", "e2"], {"e2"}))

    def test_claim_token_fence(self):
        self.assertTrue(fencing.claim_may_commit("tok", "tok"))
        self.assertFalse(fencing.claim_may_commit("tok", "other"))
        self.assertFalse(fencing.claim_may_commit(None, None))

    def test_debounce_upper_bound(self):
        self.assertEqual(fencing.next_fold_at(10, 0, 2, 5), 5)   # capped by dirty_since + max_wait
        self.assertEqual(fencing.next_fold_at(1, 0, 2, 5), 3)


if __name__ == "__main__":
    unittest.main()
