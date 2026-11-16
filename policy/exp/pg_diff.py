"""PG-DIFF: the plpgsql core (pol/_pgcore.sql) against the Python oracle (pol/_core.py), event by event.

Inputs.
  B2 logs: exp/b2_rebuild.make_log, exp.seeds "pgdiff", PGDIFF_LOGS logs per seed (random, cap and contest modes).
  B1 traces: the B1 breadth-first exploration (production rules) of every scenario at its B1 depth is rebuilt in Python with parent
    edges; the plpgsql core then replays it depth-first inside ONE transaction, with a savepoint per transition: every transition
    B1 explored is applied in Postgres and compared, and the walk descends only along the BFS tree edges. Scenario "caps" uses the
    B1 small caps (2 open per principal, 3 non-human per req) in BOTH cores (the same one-literal patch on each side).
Per event: (code, original code) equal. Per log / per tree node: the comparable view (CORE-RULES section 8) built from the rows
equals MemoryState.comparable(). Per B2 log: the applied events read back from dc.events (evid, code, in chain order) equal the
oracle's applied events.
Control (the harness must not be vacuous): the same comparison against a MUTATED plpgsql core whose stale check is removed (a
non-sibling event bound to an old version is judged as a sibling). It must produce mismatches.

Runs on the lab host only (Postgres in the experiment's own container, owner connection: the raw core is owner-only).
usage: python -m exp.pg_diff"""
import hashlib
import json
import os
import random
import time
from collections import deque

from exp import common  # noqa: F401
from exp.common import write_result
from exp.seeds import seeds
import pol._core as CORE

PER_SEED = int(os.environ.get("PGDIFF_LOGS", "60"))
SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pol", "_pgcore.sql")
STALE_OLD = "    if not coalesce(sib, false) then code := 'stale'; return; end if;\n"
STALE_NEW = "    if false then code := 'stale'; return; end if;\n"
CAPS_OLD = "x.status = 'open') >= 16\n       or (kind <> 'human' and n >= 256) then"
CAPS_NEW = "x.status = 'open') >= 2\n       or (kind <> 'human' and n >= 3) then"


def _sql_variant(patches, tag):
    with open(SQL, encoding="utf-8") as f:
        src = f.read()
    for old, new in patches:
        if src.count(old) != 1:
            raise AssertionError("pg_diff patch %s: expected one match" % tag)
        src = src.replace(old, new)
    d = os.path.join(os.path.dirname(SQL), "..", "results", "tmp")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "pgcore_%s.sql" % tag)
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)
    return p


class PG:
    def __init__(self):
        from pol import pgcore
        self.pg = pgcore
        self.port = int(os.environ["POL_PG_PORT"])

    def install(self, cfg, sql_path=None):
        self.pg._SQL_PATH = sql_path or SQL
        self.pg.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {cfg["tenant"]: cfg},
                        idp_password_file=os.environ.get("POL_PG_IDP_PASSWORD_FILE"))
        c = self.pg._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "pol-pgdiff")
        return c, self.pg.PgEngine(c, cfg["tenant"])


def _raw_apply(conn, tenant, ev):
    from psycopg.types.json import Jsonb
    conn.execute("select dc._lock_event(%s, %s)", (tenant, Jsonb(ev)))
    r = conn.execute("select dc._reduce_apply(%s, %s)", (tenant, Jsonb(ev))).fetchone()[0]
    return r["code"], r["original"]


def b2_logs():
    from exp.b2_rebuild import make_log
    out = []
    for s in seeds("pgdiff"):
        rng = random.Random(s)
        for li in range(PER_SEED):
            mode, evs = make_log(rng, s, li)
            out.append(("%s/%d" % (s, li), mode, evs))
    return out


def run_b2(pg, logs, sql_path=None, stop_at_first=False):
    from exp.core_gen import config
    res = {"logs": 0, "events": 0, "code_mismatches": 0, "view_mismatches": 0, "log_mismatches": 0, "first": None}
    for lid, mode, evs in logs:
        cfg = config()
        conn, eng = pg.install(cfg, sql_path)
        try:
            st = CORE.MemoryState(cfg)
            applied = []
            for i, ev in enumerate(evs):
                code, orig, delta = CORE.reduce(st, ev)
                st.apply_delta(delta)
                if code not in CORE.REFUSED:
                    applied.append([ev.get("id"), code])
                try:
                    got = eng._submit(ev)
                except Exception as e:      # noqa: BLE001  (a mutated core may break; the enforced arm counts it as a failure)
                    got = ("error:" + type(e).__name__, None)
                    res["errors"] = res.get("errors", 0) + 1
                res["events"] += 1
                if tuple(got) != (code, orig):
                    res["code_mismatches"] += 1
                    if res["first"] is None:
                        res["first"] = {"log": lid, "i": i, "ev": ev, "oracle": [code, orig], "pg": list(got)}
            try:
                same = eng._comparable() == st.comparable()
            except Exception:       # noqa: BLE001
                same, res["errors"] = False, res.get("errors", 0) + 1
            if not same:
                res["view_mismatches"] += 1
            rows = [[e, c] for e, c in conn.execute("select evid, code from dc.events where tenant=%s order by chain_n", (cfg["tenant"],)).fetchall()]
            if rows != applied:
                res["log_mismatches"] += 1
            res["logs"] += 1
            if not eng._verify_chain()[1]:
                res["chain_bad"] = res.get("chain_bad", 0) + 1
        finally:
            conn.close()
        if stop_at_first and res["code_mismatches"] + res["view_mismatches"]:
            break
    return res


def b1_tree(scen, depth, core):
    """The B1 exploration with parent edges: {key: (state, [(tpl_index, ev, child_key or None)])}, root key."""
    from exp import b1_modelcheck as B1
    pool = B1.POOLS[scen] if scen != "joint" else B1.POOLS["r1"] + B1.POOLS["r2"] + B1.POOLS["r3"]
    st0 = core.MemoryState(B1.config())
    k0 = B1.INV.sha(B1.snap(st0))
    seen, nodes = {k0}, {}
    frontier = deque([(k0, st0, 0)])
    while frontier:
        key, st, d = frontier.popleft()
        edges = []
        if d < depth:
            for ti, tpl in enumerate(pool):
                ev = B1.deliver(st, tpl, core)
                post, code, orig = B1.step(core, st, ev)
                k2 = B1.INV.sha(B1.snap(post))
                child = None
                if k2 not in seen:
                    seen.add(k2)
                    child = k2
                    if d + 1 <= depth:
                        frontier.append((k2, post, d + 1))
                edges.append((ev, code, orig, child))
        nodes[key] = (st, edges)
    return nodes, k0


def run_b1(pg, scen, depth, sql_path=None):
    import psycopg
    from exp import b1_modelcheck as B1
    core = B1.core_for("enforced", scen)
    nodes, root = b1_tree(scen, depth, core)
    res = {"scenario": scen, "depth": depth, "states": len(nodes), "transitions": 0, "code_mismatches": 0, "view_mismatches": 0, "first": None}
    conn, eng = pg.install(B1.config(), sql_path)
    t = B1.config()["tenant"]

    def walk(key, lvl):
        st, edges = nodes[key]
        try:
            same = eng._comparable() == st.comparable()
        except Exception:       # noqa: BLE001
            same, res["errors"] = False, res.get("errors", 0) + 1
        if not same:
            res["view_mismatches"] += 1
        for ev, code, orig, child in edges:
            try:
                with conn.transaction():
                    try:
                        with conn.transaction():
                            got = _raw_apply(conn, t, ev)
                    except psycopg.Error as e:
                        got = ("error:" + type(e).__name__, None)
                        res["errors"] = res.get("errors", 0) + 1
                    res["transitions"] += 1
                    if tuple(got) != (code, orig):
                        res["code_mismatches"] += 1
                        if res["first"] is None:
                            res["first"] = {"ev": ev, "oracle": [code, orig], "pg": list(got), "level": lvl}
                    if child is not None and child in nodes:
                        walk(child, lvl + 1)
                    raise psycopg.Rollback()
            except psycopg.Rollback:
                pass
    try:
        with conn.transaction():
            walk(root, 0)
            raise psycopg.Rollback()
    except psycopg.Rollback:
        pass
    finally:
        conn.close()
    return res


def main():
    import sys
    sys.setrecursionlimit(10000)
    from exp import b1_modelcheck as B1
    t0 = time.time()
    pg = PG()
    logs = b2_logs()
    out = {"b2": run_b2(pg, logs), "b1": []}
    print("b2", json.dumps(out["b2"], default=str)[:600], round(time.time() - t0), "s", flush=True)
    only = [x for x in os.environ.get("PGDIFF_SCEN", "").split(",") if x]          # dev smoke only; the recorded run uses all
    for scen, d in B1.DEPTH.items():
        if only and scen not in only:
            continue
        path = _sql_variant([(CAPS_OLD, CAPS_NEW)], "caps") if scen == "caps" else None
        out["b1"].append(run_b1(pg, scen, d, path))
        print("b1", json.dumps(out["b1"][-1], default=str)[:600], round(time.time() - t0), "s", flush=True)
    mut = _sql_variant([(STALE_OLD, STALE_NEW)], "ctl_no_stale")
    out["control_no_stale"] = {"b2": run_b2(pg, logs[: max(10, len(logs) // 10)], mut),
                               "b1_r2": run_b1(pg, "r2", B1.DEPTH["r2"], mut)}
    c = out["control_no_stale"]
    out["control_caught"] = (c["b2"]["code_mismatches"] + c["b2"]["view_mismatches"] + c["b1_r2"]["code_mismatches"] + c["b1_r2"]["view_mismatches"]) > 0
    b2, b1 = out["b2"], out["b1"]
    out["pass"] = (b2["code_mismatches"] == b2["view_mismatches"] == b2["log_mismatches"] == 0 and not b2.get("chain_bad") and not b2.get("errors")
                   and all(x["code_mismatches"] == x["view_mismatches"] == 0 and not x.get("errors") for x in b1) and out["control_caught"]
                   and len(b1) == len(B1.DEPTH) and b2["logs"] > 0)
    out["verdict"] = "PASS" if out["pass"] else "FAIL"
    out["judged_by"] = "this module's pass condition (no independent verify/ module; THRESHOLDS amendment 5)"
    out["seconds"] = round(time.time() - t0, 1)
    p = write_result("pg_diff", {"per_seed": PER_SEED, "b1_depth": B1.DEPTH, "seeds": seeds("pgdiff"), "scenarios_only": os.environ.get("PGDIFF_SCEN") or "all"}, out)
    print(json.dumps({k: v for k, v in out.items()}, indent=1, default=str)[:4000])
    print("wrote", p, "sha256", hashlib.sha256(open(p, "rb").read()).hexdigest()[:16])
    if not out["pass"]:
        raise SystemExit(1)                          # run_all.sh scores a nonzero exit as FAIL


if __name__ == "__main__":
    main()
