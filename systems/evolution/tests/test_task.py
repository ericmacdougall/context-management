import os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from evolution import task, scen, oracle


def ev(seq, kind, body, trust="user_typed"):
    return {"seq": seq, "kind": kind, "trust_origin": trust, "body": body}


DEF = ev(1, "task_defined", {"task_id": "T", "items": [{"id": "g", "type": "goal", "text": "ship it"}, {"id": "a", "type": "acceptance", "text": "p95 < 1s"}, {"id": "s", "type": "scope", "text": "EU only"}]})


def steps(*chains):
    out, n = [], 2
    for sid, serves, comp in chains:
        out += [ev(n, "step_planned", {"step_id": sid, "serves": serves}), ev(n + 1, "step_started", {"step_id": sid}), ev(n + 2, "step_completed", {"step_id": sid, "artifacts": ["x"]})]
        n += 3
    return out, n


class Task(unittest.TestCase):
    def test_classification_and_nothing_deleted(self):
        st, n = steps(("p", ["g"], 1), ("q", ["a"], 1), ("r", ["s"], 1))
        evs = [DEF] + st + [ev(n, "task_amended", {"amendment_id": "A1", "ops": [{"op": "change", "id": "a", "text": "p95 < 500ms"}, {"op": "remove", "id": "s"}]})]
        v = task.task_state(evs)
        cls = {r["step_id"]: r["class_now"] for r in v["steps"]}
        self.assertEqual(cls, {"p": "still_valid", "q": "needs_recheck", "r": "obsolete"})
        self.assertTrue(all(r["artifacts"] == ["x"] for r in v["steps"]))     # obsolete keeps artifacts
        self.assertEqual(task.redo_set(v), ["q"])
        self.assertEqual([r for r in v["steps"] if r["step_id"] == "q"][0]["reason"]["caused_by"], ["A1"])

    def test_revert_restores_exactly(self):
        st, n = steps(("p", ["a"], 1))
        a = ev(n, "task_amended", {"amendment_id": "A1", "ops": [{"op": "change", "id": "a", "text": "other"}]})
        r = ev(n + 1, "task_amended", {"amendment_id": "A2", "reverts": "A1", "ops": [{"op": "change", "id": "a", "text": "p95 < 1s"}]})
        before = task.task_state([DEF] + st)["steps"]
        mid = task.task_state([DEF] + st + [a])["steps"][0]["class_now"]
        after = task.task_state([DEF] + st + [a, r])["steps"]
        self.assertEqual(mid, "needs_recheck")
        self.assertEqual(before[0]["class_now"], after[0]["class_now"])

    def test_low_trust_cannot_amend(self):
        for t in ("assistant", "web", "user_supplied_external"):
            v = task.task_state([DEF, ev(2, "task_amended", {"amendment_id": "P", "ops": [{"op": "remove", "id": "g"}]}, t)])
            self.assertEqual(v["version"], 1); self.assertEqual(len(v["proposals"]), 1); self.assertIn("g", v["items"])

    def test_in_flight_basis_is_start_version(self):
        evs = [DEF, ev(2, "step_planned", {"step_id": "p", "serves": ["a"]}), ev(3, "step_started", {"step_id": "p"}),
               ev(4, "task_amended", {"amendment_id": "A1", "ops": [{"op": "change", "id": "a", "text": "new"}]}), ev(5, "step_completed", {"step_id": "p", "artifacts": []})]
        r = task.task_state(evs)["steps"][0]
        self.assertEqual((r["basis_version"], r["completion_version"], r["class_at_completion"], r["class_now"]), (1, 2, "needs_recheck", "needs_recheck"))

    def test_render_marks(self):
        st, n = steps(("p", ["a"], 1))
        s = task.fold([DEF] + st + [ev(n, "task_amended", {"amendment_id": "A1", "ops": [{"op": "change", "id": "a", "text": "p95 < 500ms"}, {"op": "remove", "id": "s"}]})])
        txt = task.render(s)
        self.assertEqual(txt.count("[CURRENT] a "), 1)
        self.assertNotIn("[CURRENT] s ", txt)
        head, sup = txt.split("SUPERSEDED", 1)
        self.assertNotIn("p95 < 1s", head); self.assertIn("p95 < 1s", sup)

    def test_oracle_agrees_and_broken_variant_is_caught(self):
        rng = random.Random(5)
        broken_diff = 0
        for k in range(150):
            sc = scen.gen_scenario(rng, immediate_revert=(k % 3 == 0))
            v = task.task_state(sc["events"]); o = oracle.oracle(sc["truth"])
            self.assertEqual({r["step_id"]: r["class_now"] for r in v["steps"]}, o)
            # broken variant: "any amendment since completion forces redo" (silent redo of still-valid work)
            last = len(sc["truth"]["snaps"])
            broken = {s: ("needs_recheck" if t["basis"] is not None and t["basis"] < last and o[s] == "still_valid" else o[s]) for s, t in sc["truth"]["steps"].items()}
            broken_diff += broken != o
        self.assertGreater(broken_diff, 50)

    def test_incremental_equals_rebuild(self):
        sc = scen.gen_scenario(random.Random(9))
        s = task.initial()
        for e in sc["events"]:
            s = task.apply(s, e)
            s = __import__("json").loads(task.canon(s))     # persisted and reloaded between events
        self.assertEqual(task.canon(s), task.canon(task.fold(sc["events"])))


if __name__ == "__main__":
    unittest.main()
