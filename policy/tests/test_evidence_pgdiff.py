"""Evidence identity properties of verify/pgdiff.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS amendment 5,
exp/pg_diff.py, exp/b1_modelcheck.py): 5 seeds x per_seed 60 B2 logs "<seed>/<li>" in arm b2, and the first max(10, logs // 10) of them
in arm ctl:b2; per log one v row and one l row (identity (arm, log)) and t rows i = 0..n-1, n declared by the l row (identity (arm, log,
i); category 2). B1: arm b1:<s> for every scenario s of b1_depth at its depth, and ctl:b1:r2 (the r2 tree on the mutated core). A B1
tree is checked by closure from its root: v rows (arm, node), node 0 at level 0; every node above the depth has exactly P(s) t rows
(arm, node, ti), ti = 0..P-1, P the scenario's template pool (36, 20, 25, 13; joint 81); every other node is the child of exactly one
t row, one level below it (category 2: a tree pruned consistently is not detected). The identity fields are new in exp/pg_diff.py,
appended to the rows: t rows of the B2 arms get i and l rows n; B1 v rows get node, B1 t rows node, ti and child. The synthetic trees
here are small (a chain to the depth, plus one more child of the root), with P(s) transitions per inner node as in the experiment."""
import unittest

from tests.evidence_props import Fam, Props, load

SEEDS, PER = [61, 62, 63, 64, 65], 60
DEPTH = {"r1": 6, "r2": 8, "r3": 8, "caps": 8, "joint": 4}
POOL = {"r1": 36, "r2": 20, "r3": 25, "caps": 13, "joint": 81}
B2_ARMS = ("b2", "ctl:b2")


def _logs(arm, lids, diff=None):
    """Two events per log: the first applied, the second refused. In ctl:b2 the log `diff` has the one semantic difference."""
    rows = []
    for lid in lids:
        pg2 = "decided_accept" if lid == diff else "stale"
        rows += [["t", arm, lid, "proposed", None, "proposed", None, 0], ["t", arm, lid, "stale", None, pg2, None, 1],
                 ["v", arm, lid, "h" + lid, "h" + lid], ["l", arm, lid, [["e0", "proposed"]], [["e0", "proposed"]], True, 2]]
    return rows


def _tree(arm, depth, pool, diff=False):
    """Root 0 with children 1 and 2 (level 1); a chain from 1 down to the depth (node L + 1 at level L >= 2). Depth-first, as written."""
    level, kids = {0: 0, 1: 1, 2: 1}, {0: {0: 1, 1: 2}}
    prev = 1
    for lv in range(2, depth + 1):
        level[lv + 1], kids[prev], prev = lv, {0: lv + 1}, lv + 1
    rows = []

    def walk(n):
        lv = level[n]
        rows.append(["v", arm, lv, "h%d" % n, "h%d" % n, n])
        if lv < depth:
            for ti in range(pool):
                c = kids.get(n, {}).get(ti)
                pg = "stale" if (diff and n == 0 and ti == 2) else "proposed"
                rows.append(["t", arm, lv, "proposed", None, pg, None, n, ti, c])
                if c is not None:
                    walk(c)
    walk(0)
    return rows


def honest(seeds=SEEDS, per=PER, depth=DEPTH):
    ids = ["%s/%d" % (s, li) for s in seeds for li in range(per)]
    ctl = ids[:max(10, len(ids) // 10)]
    rows = _logs("b2", ids)
    for s, d in depth.items():
        rows += _tree("b1:" + s, d, POOL[s])
    rows += _logs("ctl:b2", ctl, diff=ctl[3])
    rows += _tree("ctl:b1:r2", depth.get("r2", 8), POOL["r2"], diff=True)
    return rows


def result(rows, depth=DEPTH):
    def n(kind, arm):
        return sum(1 for r in rows if r[0] == kind and r[1] == arm)
    return {"verdict": "PASS", "b2": {"events": n("t", "b2"), "logs": n("l", "b2")},
            "b1": [{"scenario": s, "depth": d, "states": n("v", "b1:" + s), "transitions": n("t", "b1:" + s)} for s, d in depth.items()],
            "control_no_stale": {"b2": {"events": n("t", "ctl:b2"), "logs": n("l", "ctl:b2")},
                                 "b1_r2": {"states": n("v", "ctl:b1:r2"), "transitions": n("t", "ctl:b1:r2")}}}


def document(rows, seeds=SEEDS, per=PER, depth=DEPTH, scenarios_only="all"):
    return {"name": "pg_diff", "params": {"per_seed": per, "b1_depth": depth, "seeds": seeds, "scenarios_only": scenarios_only},
            "result": result(rows, depth)}


ROWS = honest()
DOC = document(ROWS)


def _b1(r):
    return isinstance(r[1], str) and r[1] not in B2_ARMS


def _nodes(arm):
    return sum(1 for r in ROWS if r[0] == "v" and r[1] == arm)


class PgDiffEvidence(Props, unittest.TestCase):
    V = load("pgdiff")
    FAMILIES = (Fam("b2 log", lambda r: r[0] == "l", [0, 1, 2], arm=1),
                Fam("b2 view", lambda r: r[0] == "v" and not _b1(r), [0, 1, 2], arm=1),
                Fam("b2 event", lambda r: r[0] == "t" and not _b1(r), [0, 1, 2, 7], index=7, arm=1, end=lambda s, r: 2),
                Fam("b1 node", lambda r: r[0] == "v" and _b1(r), [0, 1, 5], index=5, arm=1, end=lambda s, r: _nodes(r[1])),
                Fam("b1 transition", lambda r: r[0] == "t" and _b1(r), [0, 1, 7, 8], index=8, arm=1,
                    end=lambda s, r: POOL[r[1].split(":")[-1]]))
    SAMPLE = {"b2 log": lambda r: r[0] == "l" and r[1] == "b2" and r[2] == "63/30",
              "b2 view": lambda r: r[0] == "v" and r[1] == "b2" and r[2] == "63/30",
              "b2 event": lambda r: r[0] == "t" and r[1] == "b2" and r[2] == "63/30" and r[7] == 1,
              "b1 node": lambda r: r[0] == "v" and r[1] == "b1:r3" and r[5] == 1,
              "b1 transition": lambda r: r[0] == "t" and r[1] == "b1:r3" and r[7] == 1 and r[8] == 1}

    def doc(self):
        return DOC

    def build(self):
        return ROWS

    def sample(self, fam):
        """An enforced-arm row (so that break_row fails on its own) and another row of the family."""
        rows = self.rows()
        i = next(j for j, r in enumerate(rows) if fam.pick(r) and self.SAMPLE[fam.name](r))
        me = [rows[i][f] for f in fam.ident]
        return i, next(j for j in self.positions(fam) if [rows[j][f] for f in fam.ident] != me)

    def break_row(self, fam, row):
        r = list(row)
        if fam.name in ("b2 event", "b1 transition"):
            r[5] = "z"                      # Postgres answered another code
        elif fam.name in ("b2 view", "b1 node"):
            r[4] = "other"                  # another view hash
        else:
            r[5] = False                    # the chain does not verify
        return r

    def shrunk(self):
        ten = list(range(61, 71))
        half = {s: d // 2 for s, d in DEPTH.items()}
        no_joint = {s: d for s, d in DEPTH.items() if s != "joint"}
        out = []
        for label, kw in (("300 logs as 30 per seed over ten seeds, declared and run", {"seeds": ten, "per": 30}),
                          ("every B1 depth halved, declared and run", {"depth": half}),
                          ("scenario joint dropped from b1_depth with its rows", {"depth": no_joint})):
            rows = honest(**kw)
            out.append((label, document(rows, **kw), rows))
        return out

    # ------------------------------------------------------------------------------------------------ B2 log ids
    def _moved(self, arm, old, new):
        return [r[:2] + [new] + r[3:] if r[1] == arm and r[2] == old else r for r in ROWS]

    def test_log_moved_to_an_undeclared_id(self):
        """b2 log 65/59 (its events, view and log rows) moved to 65/60, past per_seed: still 300 distinct logs, so ea4677a PASSED."""
        self.bad(self._moved("b2", "65/59", "65/60"), "b2 log 65/59 moved to 65/60", ("missing identity", "extra identity"))

    def test_log_moved_to_an_undeclared_seed(self):
        self.bad(self._moved("b2", "62/7", "99/7"), "b2 log 62/7 moved to seed 99", ("missing identity", "extra identity"))

    def test_control_log_outside_the_first_tenth(self):
        """ctl:b2 runs the first max(10, logs // 10) logs of the run order; a control log moved to another log is not that design."""
        self.bad(self._moved("ctl:b2", "61/29", "63/0"), "ctl:b2 log 61/29 moved to 63/0", ("missing identity", "extra identity"))

    # ------------------------------------------------------------------------------------------------ B1 trees
    def _edit(self, pick, change):
        return [change(list(r)) if pick(r) else r for r in ROWS]

    def test_child_declared_twice(self):
        """Root edge 1 of b1:r2 points at node 1 (already root edge 0's child) instead of node 2."""
        rows = self._edit(lambda r: r[0] == "t" and r[1] == "b1:r2" and r[7] == 0 and r[8] == 1, lambda r: r[:9] + [1])
        self.bad(rows, "b1:r2 node 1 declared as the child of two transitions", ("extra identity", "child of 2 transitions"))

    def test_child_cleared(self):
        """Root edge 1 of b1:caps declares no child, but node 2 and its transitions are still there."""
        rows = self._edit(lambda r: r[0] == "t" and r[1] == "b1:caps" and r[7] == 0 and r[8] == 1, lambda r: r[:9] + [None])
        self.bad(rows, "b1:caps node 2 not declared by any transition", ("extra identity",))

    def test_subtree_dropped_under_a_declaring_parent(self):
        """b1:r1 node 2 and its 36 transitions dropped; the summary edited to match (so ea4677a PASSED); root edge 1 still declares it."""
        rows = [r for r in ROWS if not (r[1] == "b1:r1" and ((r[0] == "v" and r[5] == 2) or (r[0] == "t" and r[7] == 2)))]
        self.bad(rows, "b1:r1 node 2 dropped with its transitions", ("missing identity",), doc=document(rows))

    def test_inner_node_relabelled_a_leaf(self):
        """b1:joint node 2 (level 1) given the depth's level, and its 81 transitions dropped; summary edited to match."""
        rows = [r for r in ROWS if not (r[0] == "t" and r[1] == "b1:joint" and r[7] == 2)]
        rows = [r[:2] + [DEPTH["joint"]] + r[3:] if (r[0] == "v" and r[1] == "b1:joint" and r[5] == 2) else r for r in rows]
        self.bad(rows, "b1:joint node 2 moved to level 4 without its transitions", ("level",), doc=document(rows))

    def test_transition_moved_to_another_level(self):
        rows = self._edit(lambda r: r[0] == "t" and r[1] == "b1:r3" and r[7] == 3 and r[8] == 5, lambda r: r[:2] + [7] + r[3:])
        self.bad(rows, "a b1:r3 transition of node 3 (level 2) written at level 7", ("level",))


if __name__ == "__main__":
    unittest.main()
