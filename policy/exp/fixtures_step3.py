"""Step-3 hand-off fixtures (GoodQA #16096): B2 logs and B1 trees of the decision core with the Python oracle's expected outcomes, for
the port of the core into the write path's transaction (COMPOSITION.md, HANDOFF-STEP3.md). NOT proof and not an experiment: no
verdict, not in run_all.sh, no seed of THRESHOLDS.md (the fixture seeds are the strings step3-b2-<n>).

  python -m exp.fixtures_step3 export <dir> [--full]
      writes b2.jsonl.gz, b1.jsonl.gz and MANIFEST.json (sha256 and counts of both files).
  python -m exp.fixtures_step3 check <dir> [--core <B1 control name>] [--pg]
      checks the files against MANIFEST.json, then replays every fixture through the oracle pol._core (or, with --core, through that
      B1 control's mutated oracle; a correct checker must then report mismatches), and with --pg through the dc core in Postgres (the
      reference implementation the port replaces; needs the POL_PG_* environment of hostrun.sh). Exit 0 only if every check holds.

Formats (gzip, one canonical JSON object per line: sorted keys, compact separators):
  b2.jsonl.gz, one line per log:
    {"log": id, "mode": m, "config": cfg, "events": [[ev, code, original], ...], "applied": [[evid, code], ...], "view": comparable,
     "view_sha": sha256 of the canonical comparable view}
    events are submitted in order on a fresh tenant; "applied" lists the accepted events in chain order; "view" is the comparable
    view (CORE-RULES.md section 8) after the last event.
  b1.jsonl.gz, one line per scenario:
    {"scenario": s, "depth": d, "small_caps": bool, "config": cfg, "root": key,
     "nodes": {key: {"level": n, "view_sha": hex, "edges": [[ev, code, original, child key or null], ...]}}}
    the breadth-first exploration of exp/b1_modelcheck.py: from a node's state, each edge's event is applied ALONE (then rolled back);
    child is the node the event leads to when that state was first reached through this edge (the BFS tree), else null. Keys are
    b1_modelcheck's state hashes. small_caps: both cores run with the B1 small caps (2 open per principal, 3 non-human per req).
"""
import gzip
import hashlib
import json
import os
import random
import sys
from collections import deque

from exp import common  # noqa: F401
import pol._core as CORE

B2_SEEDS = ["step3-b2-%d" % i for i in (1, 2, 3)]
B2_LOGS_PER_SEED = 20                                                 # compact set (committed, about 0.65 MB); --full: 60
B1_DEPTHS = {"r1": 3, "r2": 4, "r3": 4, "caps": 5, "joint": 2}       # compact set (B1_QUICK depths); --full: b1_modelcheck.DEPTH
FULL_LOGS_PER_SEED = 60


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def vsha(view):
    return hashlib.sha256(canon(view).encode()).hexdigest()


def _sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write(path, objs):
    """gzip with mtime 0 and no file name, so the same content always gives the same bytes and sha256."""
    n = 0
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as g:
        for o in objs:
            g.write((canon(o) + "\n").encode("utf-8"))
            n += 1
    return n


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


# ------------------------------------------------------------------------------------------------ export

def b2_records(seeds=None, per_seed=None):
    from exp.b2_rebuild import make_log
    from exp.core_gen import config
    for s in seeds or B2_SEEDS:
        rng = random.Random(s)
        for li in range(per_seed or B2_LOGS_PER_SEED):
            mode, evs = make_log(rng, s, li)
            cfg = config()
            st = CORE.MemoryState(cfg)
            rows, applied = [], []
            for ev in evs:
                code, orig, delta = CORE.reduce(st, ev)
                st.apply_delta(delta)
                rows.append([ev, code, orig])
                if code not in CORE.REFUSED:
                    applied.append([ev.get("id"), code])
            view = st.comparable()
            yield {"log": "%s/%d" % (s, li), "mode": mode, "config": cfg, "events": rows, "applied": applied, "view": view,
                   "view_sha": vsha(view)}


def b1_records(depths=None):
    from exp import b1_modelcheck as B1
    from exp.pg_diff import b1_tree
    for scen, d in (depths or B1_DEPTHS).items():
        core = B1.core_for("enforced", scen)
        nodes, root = b1_tree(scen, d, core)
        level, q = {root: 0}, deque([root])
        while q:                                            # levels along the BFS tree edges
            k = q.popleft()
            for _ev, _c, _o, child in nodes[k][1]:
                if child is not None and child in nodes and child not in level:
                    level[child] = level[k] + 1
                    q.append(child)
        out = {}
        for k, (st, edges) in nodes.items():
            out[k] = {"level": level.get(k), "view_sha": vsha(st.comparable()),
                      "edges": [[ev, code, orig, child if child in nodes else None] for ev, code, orig, child in edges]}
        yield {"scenario": scen, "depth": d, "small_caps": scen == "caps", "config": B1.config(), "root": root, "nodes": out}


def export(out_dir, b2_seeds=None, per_seed=None, b1_depths=None):
    commit = _commit()                                        # before writing: the files written below would make the tree dirty
    os.makedirs(out_dir, exist_ok=True)
    p2, p1 = os.path.join(out_dir, "b2.jsonl.gz"), os.path.join(out_dir, "b1.jsonl.gz")
    recs2 = list(b2_records(b2_seeds, per_seed))
    _write(p2, recs2)
    recs1 = list(b1_records(b1_depths))
    _write(p1, recs1)
    man ={"generator": "policy/exp/fixtures_step3.py", "commit": commit,
           "b2": {"file": "b2.jsonl.gz", "sha256": _sha_file(p2), "logs": len(recs2), "events": sum(len(r["events"]) for r in recs2),
                  "seeds": list(b2_seeds or B2_SEEDS), "logs_per_seed": per_seed or B2_LOGS_PER_SEED},
           "b1": {"file": "b1.jsonl.gz", "sha256": _sha_file(p1), "scenarios": {r["scenario"]: {
               "depth": r["depth"], "nodes": len(r["nodes"]), "edges": sum(len(n["edges"]) for n in r["nodes"].values())} for r in recs1}},
           "not_proof": "development fixtures for the step-3 port; the proof of the core is the stamped run of THRESHOLDS.md"}
    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(man, sort_keys=True, indent=1) + "\n")
    return man


def _commit():
    """HEAD, prefixed "dirty:" when the code that determines the fixtures (exp/ and pol/) differs from it; the output directory does
    not count. On a synced host without .git: GIT_STAMP.json's commit. None if neither is available."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "exp", "pol"], cwd=root, capture_output=True, text=True,
                               check=True).stdout.strip()
        return ("dirty:" if dirty else "") + head
    except (OSError, subprocess.CalledProcessError):
        p = os.path.join(root, "GIT_STAMP.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                g = json.load(f)
            return ("dirty:" if g.get("dirty") else "") + str(g.get("commit"))
        return None


# ------------------------------------------------------------------------------------------------ check

class Report:
    def __init__(self):
        self.counts = {"b2_logs": 0, "b2_events": 0, "b1_scenarios": 0, "b1_edges": 0, "b1_nodes": 0}
        self.mismatches = 0
        self.first = []

    def bad(self, what):
        self.mismatches += 1
        if len(self.first) < 5:
            self.first.append(what)


def check_manifest(d, rep):
    with open(os.path.join(d, "MANIFEST.json"), encoding="utf-8") as f:
        man = json.load(f)
    for part in ("b2", "b1"):
        got = _sha_file(os.path.join(d, man[part]["file"]))
        if got != man[part]["sha256"]:
            rep.bad({"manifest": part, "want": man[part]["sha256"], "got": got})
    return man


def _oracle(control, scen=None, small_caps=False):
    from exp import b1_modelcheck as B1
    if control is None:
        return B1.core_for("enforced", "caps" if small_caps else (scen or "r1"))
    if scen is not None:
        return B1.core_for(control, "caps" if small_caps else scen)
    m, old, new = B1.CTL_BY_NAME[control][1]
    return B1.multi_variant(m, [(old, new)], "fx_%s" % control)


def check_oracle(d, control=None, rep=None):
    from exp import b1_modelcheck as B1
    rep = rep or Report()
    core2 = _oracle(control)
    for r in _read(os.path.join(d, "b2.jsonl.gz")):
        st = core2.MemoryState(r["config"])
        applied = []
        for i, (ev, code, orig) in enumerate(r["events"]):
            c, o, delta = core2.reduce(st, ev)
            st.apply_delta(delta)
            rep.counts["b2_events"] += 1
            if (c, o) != (code, orig):
                rep.bad({"b2": r["log"], "i": i, "want": [code, orig], "got": [c, o]})
            if c not in core2.REFUSED:
                applied.append([ev.get("id"), c])
        if applied != r["applied"]:
            rep.bad({"b2": r["log"], "applied": "differs"})
        if vsha(st.comparable()) != r["view_sha"] or vsha(r["view"]) != r["view_sha"]:
            rep.bad({"b2": r["log"], "view": "differs"})
        rep.counts["b2_logs"] += 1
    for r in _read(os.path.join(d, "b1.jsonl.gz")):
        core = _oracle(control, r["scenario"], r["small_caps"])
        nodes = r["nodes"]
        states = {r["root"]: core.MemoryState(r["config"])}
        q = deque([r["root"]])
        while q:
            k = q.popleft()
            st, node = states[k], nodes[k]
            rep.counts["b1_nodes"] += 1
            if vsha(st.comparable()) != node["view_sha"]:
                rep.bad({"b1": r["scenario"], "node": k, "view": "differs"})
            for ev, code, orig, child in node["edges"]:
                post, c, o = B1.step(core, st, ev)
                rep.counts["b1_edges"] += 1
                if (c, o) != (code, orig):
                    rep.bad({"b1": r["scenario"], "level": node["level"], "ev": ev.get("id"), "want": [code, orig], "got": [c, o]})
                if child is not None and child not in states:
                    if B1.INV.sha(B1.snap(post)) != child:
                        rep.bad({"b1": r["scenario"], "edge_to": child, "state": "differs"})
                    states[child] = post
                    q.append(child)
        if len(states) != len(nodes):
            rep.bad({"b1": r["scenario"], "reached": len(states), "nodes": len(nodes)})
        rep.counts["b1_scenarios"] += 1
    return rep


def check_pg(d, rep=None, patches=()):
    """Replay through the dc core in Postgres: B2 by the session-free raw engine (PgEngine._submit), B1 by dc._lock_event +
    dc._reduce_apply inside savepoints, as exp/pg_diff.py does. The port must pass the same replay against the write-path schema.
    patches: (old, new) replacements of pol/_pgcore.sql, each applying exactly once (a mutated core, for the checker's own control)."""
    import psycopg
    from exp.pg_diff import CAPS_NEW, CAPS_OLD, PG, _raw_apply, _sql_variant
    rep = rep or Report()
    pg = PG()
    tag = "fx%d" % len(patches)
    base_sql = _sql_variant(list(patches), tag) if patches else None
    caps_sql = None
    for r in _read(os.path.join(d, "b2.jsonl.gz")):
        conn, eng = pg.install(r["config"], base_sql)
        try:
            for i, (ev, code, orig) in enumerate(r["events"]):
                try:
                    got = tuple(eng._submit(ev))
                except Exception as e:  # noqa: BLE001
                    got = ("error:" + type(e).__name__, None)
                rep.counts["b2_events"] += 1
                if got != (code, orig):
                    rep.bad({"pg_b2": r["log"], "i": i, "want": [code, orig], "got": list(got)})
            rows = [[e, c] for e, c in conn.execute("select evid, code from dc.events where tenant=%s order by chain_n",
                                                    (r["config"]["tenant"],)).fetchall()]
            if rows != r["applied"]:
                rep.bad({"pg_b2": r["log"], "applied": "differs"})
            if vsha(eng._comparable()) != r["view_sha"]:
                rep.bad({"pg_b2": r["log"], "view": "differs"})
            rep.counts["b2_logs"] += 1
        finally:
            conn.close()
    for r in _read(os.path.join(d, "b1.jsonl.gz")):
        if r["small_caps"] and caps_sql is None:
            caps_sql = _sql_variant(list(patches) + [(CAPS_OLD, CAPS_NEW)], tag + "caps")
        conn, eng = pg.install(r["config"], caps_sql if r["small_caps"] else base_sql)
        t, nodes = r["config"]["tenant"], r["nodes"]

        def walk(k):
            node = nodes[k]
            rep.counts["b1_nodes"] += 1
            if vsha(eng._comparable()) != node["view_sha"]:
                rep.bad({"pg_b1": r["scenario"], "node": k, "view": "differs"})
            for ev, code, orig, child in node["edges"]:
                try:
                    with conn.transaction():
                        try:
                            with conn.transaction():
                                got = tuple(_raw_apply(conn, t, ev))
                        except psycopg.Error as e:
                            got = ("error:" + type(e).__name__, None)
                        rep.counts["b1_edges"] += 1
                        if got != (code, orig):
                            rep.bad({"pg_b1": r["scenario"], "level": node["level"], "ev": ev.get("id"), "want": [code, orig], "got": list(got)})
                        if child is not None and child in nodes and nodes[child]["level"] == node["level"] + 1:
                            walk(child)
                        raise psycopg.Rollback()
                except psycopg.Rollback:
                    pass
        try:
            with conn.transaction():
                walk(r["root"])
                raise psycopg.Rollback()
        except psycopg.Rollback:
            pass
        finally:
            conn.close()
        rep.counts["b1_scenarios"] += 1
    return rep


def main(argv):
    if len(argv) < 3 or argv[1] not in ("export", "check"):
        sys.exit(__doc__)
    d = argv[2]
    sys.setrecursionlimit(10000)
    if argv[1] == "export":
        if "--full" in argv:                                   # the depths of the stamped B1 and pg_diff (1.17 M transitions; not committed)
            from exp import b1_modelcheck as B1
            man = export(d, None, FULL_LOGS_PER_SEED, dict(B1.DEPTH))
        else:
            man = export(d)
        print(json.dumps(man, sort_keys=True, indent=1))
        return
    control = argv[argv.index("--core") + 1] if "--core" in argv else None
    rep = Report()
    check_manifest(d, rep)
    check_oracle(d, control, rep)
    out = {"oracle": control or "pol._core", "counts": dict(rep.counts), "mismatches": rep.mismatches, "first": rep.first}
    if "--pg" in argv:
        prep = check_pg(d)
        out["pg"] = {"counts": prep.counts, "mismatches": prep.mismatches, "first": prep.first}
    print(json.dumps(out, sort_keys=True, indent=1, default=str))
    ok = rep.mismatches == 0 and out.get("pg", {}).get("mismatches", 0) == 0
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv)
