"""Evidence identity properties of verify/e5.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS E5 and its
amendments, exp/e5_threads.py, exp/threads_world.py): 5 seeds x per_seed 200 worlds per arm. Family A (A_enforced and two controls):
world rows (arm, seed, i); per world one view row per viewer of TW.viewers: p0..p(k-1) (one per thread group, k = 2 or 3 threads),
pall, pnone, pt. k is data dependent, so the world row declares its viewer list in "viewers" (category 2, parent-declared, bounded by
the two forms the design allows); identity (arm, seed, i, viewer id). "viewers" is new in exp/e5_threads.py: the stamped world rows
lack it and the next stamped run must regenerate them. Family B (the two notice arms and three controls): dworld rows (arm, seed, i)
with the arm's pinned notice; one dview row per human h0..h3 (fixed by the design), identity (arm, seed, i, viewer id)."""
import hashlib
import json
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS, PER = [61, 62, 63, 64, 65], 200
ARMS_A = ["A_enforced", "A_ctl_derive_join_dropped", "A_ctl_xref_without_decision"]
ARMS_B = {"B_notice_none": "none", "B_notice_existence_only": "existence_only", "B_ctl_flag_always": "none", "B_ctl_unscoped_value": "none",
          "B_ctl_no_read_check": "none"}
DOC = {"name": "e5_threads", "params": {"seeds": SEEDS, "per_seed": PER}, "result": {"verdict": "PASS"}}
PUB = {"tenants": ["t1"], "acl": [], "sensitivity": 0, "restricted": False, "regions": None}
SECRET = {"tenants": ["t1"], "acl": [["gb"]], "sensitivity": 2, "restricted": False, "regions": None}
HUMANS = {"h0": {"groups": [], "clearance": 0}, "h1": {"groups": ["gb"], "clearance": 3}, "h2": {"groups": ["ga"], "clearance": 3},
          "h3": {"groups": ["gb"], "clearance": 1}}          # only h1 reads the secret


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _view(row, blob):
    return dict(row, blob=blob, blob_sha256=_sha(blob), mutated_sha256=_sha(blob))


def _reads(label, v):
    return label["tenants"] == [v["tenant"]] and all(set(c) & set(v["groups"]) for c in label["acl"])


def world_a(arm, s, i):
    gs = ["ga", "gb", "gc"][:2 + i % 2]                   # 2 or 3 threads
    viewers = [("p%d" % j, "t1", [g]) for j, g in enumerate(gs)] + [("pall", "t1", gs), ("pnone", "t1", []), ("pt", "t2", [])]
    sources = [{"tokens": ["REC-%s-%d" % (s, i)], "labels": [{"tenants": ["t1"], "acl": [["ga"]]}]},
               {"tokens": ["FIND-%s-%d" % (s, i)], "labels": [{"tenants": ["t1"], "acl": [["ga"]]}, {"tenants": ["t1"], "acl": [["gb"]]}]},
               {"tokens": ["OTHER-%s-%d" % (s, i)], "labels": [{"tenants": ["t2"], "acl": []}]}]
    rows = [{"t": "world", "fam": "A", "arm": arm, "seed": s, "i": i, "edges": [["thr0", "thr1", "conflicts_with"]], "findings": 1,
             "sources": sources, "viewers": [v[0] for v in viewers]}]
    for vid, ten, groups in viewers:
        v = {"id": vid, "tenant": ten, "groups": groups}
        seen = [t for src in sources if all(_reads(lb, v) for lb in src["labels"]) for t in src["tokens"]]
        if arm != "A_enforced" and vid == "p0":
            seen = [t for src in sources for t in src["tokens"]]            # the control leaks everything to p0
        rows.append(_view({"t": "view", "fam": "A", "arm": arm, "seed": s, "i": i, "viewer": v},
                          json.dumps({"seen": seen, "xrefs": [{"edge": "conflicts_with"}]}, sort_keys=True)))
    return rows


def world_b(arm, s, i):
    notice = ARMS_B[arm]
    pub, sec = "value MK-pub-%s-%d" % (s, i), "value MK-sec-%s-%d" % (s, i)
    truth = {"r1": [{"role": "head", "ref": "v1", "label": PUB, "text": pub}, {"role": "open", "ref": "v2", "label": SECRET, "text": sec}]}
    rows = [{"t": "dworld", "fam": "B", "arm": arm, "seed": s, "i": i, "notice": notice, "authority": {"r1": ["h0", "h1"]}, "truth": truth}]
    for h, a in sorted(HUMANS.items()):
        v = dict(a, restricted_ok=False, region="eu", id=h)
        can = a["clearance"] >= 2 and "gb" in a["groups"]
        ex, tb = {"values": [pub] + ([sec] if can or arm == "B_ctl_no_read_check" else [])}, {"req": "r1"}
        if notice == "existence_only" or arm == "B_ctl_flag_always":
            ex["hidden_candidates"] = tb["hidden_candidates"] = not can
        rows.append(_view({"t": "dview", "fam": "B", "arm": arm, "seed": s, "i": i, "notice": notice, "viewer": v},
                          json.dumps({"explain": {"r1": ex}, "table": [tb]}, sort_keys=True)))
    return rows


def honest(seeds=SEEDS, per=PER, arms_b=tuple(ARMS_B)):
    rows = []
    for a in ARMS_A:
        for s in seeds:
            for i in range(per):
                rows += world_a(a, s, i)
    for a in arms_b:
        for s in seeds:
            for i in range(per):
                rows += world_b(a, s, i)
    return rows


ROWS = honest()


class E5Evidence(Props, unittest.TestCase):
    V = load("e5")
    FAMILIES = tuple(Fam(n, (lambda n: lambda r: r["t"] == n)(n), ["t", "arm", "seed", "i"], index="i", seed="seed", arm="arm",
                         end=lambda s, r: PER) for n in ("world", "view", "dworld", "dview"))
    SAMPLE = {"world": ("A_enforced", None), "view": ("A_enforced", "pall"), "dworld": ("B_notice_none", None), "dview": ("B_notice_existence_only", "h2")}

    def doc(self):
        return DOC

    def build(self):
        return ROWS

    def sample(self, fam):
        """A row of an enforced arm, seed 63, world 101 (so that break_row fails on its own), and a row of another world."""
        arm, vid = self.SAMPLE[fam.name]
        rows = self.rows()
        i = next(j for j, r in enumerate(rows) if r["t"] == fam.name and r["arm"] == arm and r["seed"] == 63 and r["i"] == 101
                 and (vid is None or r["viewer"]["id"] == vid))
        return i, next(j for j in self.positions(fam) if (rows[j]["arm"], rows[j]["seed"], rows[j]["i"]) != (arm, 63, 101))

    def break_row(self, fam, row):
        if fam.name == "world":
            return dict(row, findings=0)                                     # findings per world fall below 1
        if fam.name == "dworld":
            t = dict(row["truth"])
            t["r1"] = [t["r1"][0], dict(t["r1"][1], text=t["r1"][0]["text"])]   # the secret's marker is the public one: a leak
            return dict(row, truth=t)
        return dict(row, mutated_sha256="0" * 64)                            # the mutated world changed the view: interference

    def shrunk(self):
        ten = list(range(61, 71))
        return [("100 worlds per seed over ten seeds declared and run", dict(DOC, params=dict(DOC["params"], seeds=ten, per_seed=100)),
                 honest(seeds=ten, per=100))]

    # ------------------------------------------------------------------------------------------------ viewers
    def _pick(self, t, arm, s, i, vid=None):
        return lambda r: r["t"] == t and r["arm"] == arm and r["seed"] == s and r["i"] == i and (vid is None or r["viewer"]["id"] == vid)

    def test_view_relabelled_to_another_viewer_of_its_world(self):
        p = self._pick("view", "A_ctl_derive_join_dropped", 64, 10, "p0")
        rows = [dict(r, viewer=dict(r["viewer"], id="pall")) if p(r) else r for r in ROWS]
        self.bad(rows, "the leaking p0 view relabelled pall", ("duplicate identity", "missing identity"))

    def test_view_of_an_undeclared_viewer(self):
        """World 64/10 has 2 threads (p0, p1): a view of p2 is not in its declared list."""
        j = next(x for x, r in enumerate(ROWS) if self._pick("view", "A_enforced", 64, 10, "p1")(r))
        self.bad(ROWS + [dict(ROWS[j], viewer=dict(ROWS[j]["viewer"], id="p2"))], "a p2 view in a 2-thread world", ("extra identity",))

    def test_world_declares_a_viewer_list_the_design_does_not_have(self):
        """World 62/7 declares no pt (the other tenant's viewer) and its pt view is gone: ea4677a PASSED (views are not counted)."""
        rows = [dict(r, viewers=[v for v in r["viewers"] if v != "pt"]) if self._pick("world", "A_enforced", 62, 7)(r) else r
                for r in ROWS if not self._pick("view", "A_enforced", 62, 7, "pt")(r)]
        self.bad(rows, "world 62/7 without its pt viewer", ("viewers",))

    def test_world_declares_four_threads(self):
        j = next(x for x, r in enumerate(ROWS) if self._pick("view", "A_enforced", 65, 3, "p2")(r))
        rows = [dict(r, viewers=["p0", "p1", "p2", "p3", "pall", "pnone", "pt"]) if self._pick("world", "A_enforced", 65, 3)(r) else r
                for r in ROWS] + [dict(ROWS[j], viewer=dict(ROWS[j]["viewer"], id="p3"))]
        self.bad(rows, "world 65/3 with a fourth thread viewer", ("viewers",))

    def test_dview_of_an_undeclared_human(self):
        j = next(x for x, r in enumerate(ROWS) if self._pick("dview", "B_notice_none", 61, 5, "h3")(r))
        self.bad(ROWS + [dict(ROWS[j], viewer=dict(ROWS[j]["viewer"], id="h4"))], "a dview of h4", ("extra identity",))

    # ------------------------------------------------------------------------------------------------ arms
    def test_informational_arm_dropped(self):
        """B_ctl_unscoped_value is informational (no floor), so ea4677a PASSED without it: the design still runs it."""
        rows = [r for r in ROWS if r["arm"] != "B_ctl_unscoped_value"]
        self.bad(rows, "arm B_ctl_unscoped_value dropped", ("missing identity",))

    def test_world_notice_relabelled(self):
        """A B_ctl_flag_always world relabelled existence_only (with its views): its key-rule break disappears; the floor still holds."""
        p = lambda r: r["arm"] == "B_ctl_flag_always" and r["seed"] == 61 and r["i"] < 50   # noqa: E731
        rows = [dict(r, notice="existence_only") if p(r) else r for r in ROWS]
        self.bad(rows, "50 B_ctl_flag_always worlds relabelled existence_only", ("notice",))


if __name__ == "__main__":
    unittest.main()
