"""F1 scenarios + F2 plumbing. usage: f1_f2.py [scenarios=2000] [crash_runs=100]"""
import json, os, random, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import Counter
from evolution import evo, task, scen, oracle, taskview
from memsys import db

PY = sys.executable


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    CR = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rng = random.Random(16096)
    out = {"workload": f"{N} generated scenarios, seed 16096: 5-30 items, 10-200 steps, 1-5 amendments (narrow/widen/change acceptance/add constraint/remove/revert/same item twice) plus invalid ops and low-trust amendment attempts; 30% have an amendment immediately reverted"}
    fails = Counter(); tot = Counter(); sizes = {"items": [99, 0], "steps": [999, 0], "versions": [99, 0]}
    conn = evo.connect(); evo.reset(conn); taskview.setup(conn)
    pg_n = 0
    t0 = time.time()
    for k in range(N):
        sc = scen.gen_scenario(rng, immediate_revert=(k % 10 < 3))
        evs, truth = sc["events"], sc["truth"]
        s = task.fold(evs); v = task.view(s); o = oracle.oracle(truth)
        tot["scenarios"] += 1; tot["events"] += len(evs); tot["steps"] += len(truth["steps"]); tot["effective_versions"] += len(truth["snaps"]) - 1
        for key, val in (("items", sc["n_items"]), ("steps", sc["n_steps"]), ("versions", len(truth["snaps"]))):
            sizes[key][0] = min(sizes[key][0], val); sizes[key][1] = max(sizes[key][1], val)
        tot["reverts"] += sum(1 for e in evs if e["kind"] == "task_amended" and e["body"].get("reverts"))
        tot["low_trust_attempts"] += sc["n_low_trust"]
        # (a) nothing lost: every step with a completion event is present with its artifacts
        comp = {e["body"]["step_id"]: e["body"].get("artifacts", []) for e in evs if e["kind"] == "step_completed"}
        got = {r["step_id"]: r for r in v["steps"]}
        if any(sid not in got or got[sid]["artifacts"] != a or got[sid]["status"] != "completed" for sid, a in comp.items()):
            fails["a_step_lost"] += 1
        if sum(1 for r in v["steps"] if r["class_now"] == "obsolete") and any(got[sid]["artifacts"] != a for sid, a in comp.items()):
            fails["a_obsolete_lost_artifacts"] += 1
        tot["obsolete_steps"] += sum(r["class_now"] == "obsolete" for r in v["steps"]); tot["needs_recheck_steps"] += sum(r["class_now"] == "needs_recheck" for r in v["steps"])
        tot["still_valid_steps"] += sum(r["class_now"] == "still_valid" for r in v["steps"])
        # (b) oracle
        if {sid: r["class_now"] for sid, r in got.items()} != o:
            fails["b_oracle_mismatch"] += 1
        # (c) revert restores classifications exactly
        if sc["immediate"]:
            b, a = sc["immediate"]
            vb, va = task.view(task.fold(evs[:b])), task.view(task.fold(evs[:a]))
            tot["immediate_reverts_checked"] += 1
            cb = {r["step_id"]: r["class_now"] for r in vb["steps"] if r["status"] == "completed"}
            ca = {r["step_id"]: r["class_now"] for r in va["steps"] if r["step_id"] in cb}
            if cb != ca or task.canon(vb["items"]) != task.canon(va["items"]):
                fails["c_revert_not_exact"] += 1
        # (d) rebuild from events alone == incremental with persisted/reloaded state at random chunk boundaries
        inc = task.initial()
        for e in evs:
            inc = task.apply(inc, e, inplace=True)
            if rng.random() < 0.1:
                inc = json.loads(task.canon(inc))
        if task.canon(task.view(inc)) != task.canon(v):
            fails["d_rebuild_mismatch"] += 1
        # (e) redo set is exactly the oracle's needs_recheck set (never more, never fewer)
        want = sorted(sid for sid, c in o.items() if c == "needs_recheck")
        if task.redo_set(v) != want:
            fails["e_redo_set_mismatch"] += 1
        tot["redo_steps"] += len(want)
        # F2 context block checks
        txt = task.render(s)
        head, rest = txt.split("SUPERSEDED (", 1)
        sup, rest2 = rest.split("\nPROGRESS", 1)
        cur_ids = sorted(truth["snaps"][-1])
        if sorted(l.split(" ")[1] for l in head.splitlines() if l.startswith("[CURRENT] ")) != cur_ids or any(head.count(f"[CURRENT] {i} ") != 1 for i in cur_ids) or sum(l.startswith("[CURRENT] ") for l in txt.splitlines()) != len(cur_ids):
            fails["f2_current_items_not_exactly_once"] += 1
        removed = [i for i in truth["snaps"][0] if i not in truth["snaps"][-1]]
        if any(f"[CURRENT] {i} " in txt for i in removed):
            fails["f2_removed_marked_current"] += 1
        if len(truth["snaps"]) > 1:
            prev, cur = truth["snaps"][-2], truth["snaps"][-1]
            for i, old in prev.items():
                if (i not in cur or cur[i] != old):
                    tot["f2_superseded_texts_checked"] += 1
                    if txt.count(old) != 1 or old not in sup or old in head or old in rest2:
                        # old text may legitimately equal a text still current elsewhere only if reverted; count as failure
                        fails["f2_old_text_outside_superseded"] += 1
        if txt.count("[PROPOSAL]") != sc["n_low_trust"] or len(v["proposals"]) != sc["n_low_trust"]:
            fails["f2_proposal_count"] += 1
        if any(f"[CURRENT]" in l and "PROPOSED-" in l for l in txt.splitlines()):
            fails["f2_proposal_marked_current"] += 1
        if txt != task.render(task.fold(evs)):
            fails["f2_render_not_deterministic"] += 1
        # PG round trip (first 300): events stored encrypted, rebuilt from the log alone
        if pg_n < 300:
            sess = f"sc{k}"
            taskview.append_many(conn, "ev", sess, evs)
            back = taskview.load_events(conn, "ev", sess)
            if task.canon(task.view(task.fold(back))) != task.canon(v):
                fails["d_pg_roundtrip_mismatch"] += 1
            taskview.update_view(conn, "ev", sess)
            st, _ = taskview.read_view(conn, "ev", sess)
            if task.canon(task.view(st)) != task.canon(v):
                fails["d_pg_view_mismatch"] += 1
            pg_n += 1
    out["f1"] = {"totals": dict(tot), "ranges_seen": sizes, "failures": dict(fails), "pg_roundtrips": pg_n, "elapsed_s": round(time.time() - t0, 1)}
    out["f1_f2_pass"] = not fails and tot["immediate_reverts_checked"] > 0
    print(json.dumps(out, indent=1), flush=True)
    # ---- F2 crash between append and view update
    crash = {"runs": 0, "mismatch": 0, "lagging_before_recovery": 0, "by_point": {}}
    points = ["after_append", "mid_update", "after_update"]
    for r in range(CR):
        pt = points[r % 3]
        sc = scen.gen_scenario(random.Random(1000 + r))
        sess = f"crash{r}"
        taskview.append_many(conn, "ev", sess, sc["events"][:60])
        taskview.update_view(conn, "ev", sess)
        item = sorted(taskview.read_view(conn, "ev", sess)[0]["items"])[0]     # an item that still exists, so the amendment is effective
        p = subprocess.run([PY, "-m", "evolution.crash_child", "ev", sess, pt, item], capture_output=True, cwd=evo.ROOT)
        assert p.returncode == -9, (p.returncode, p.stderr[-300:])
        _, applied = taskview.read_view(conn, "ev", sess)
        mx = conn.execute("select max(seq) from events where tenant_id='ev' and session_id=%s", (sess,)).fetchone()[0]
        crash["lagging_before_recovery"] += applied < mx
        taskview.update_view(conn, "ev", sess)                 # restart
        st, applied = taskview.read_view(conn, "ev", sess)
        want = task.fold(taskview.load_events(conn, "ev", sess))
        ok = task.canon(st) == task.canon(want) and applied == mx and any(a["id"] == "CRASH-" + pt for a in st["amendments"])
        crash["runs"] += 1; crash["mismatch"] += not ok
        b = crash["by_point"].setdefault(pt, {"runs": 0, "mismatch": 0}); b["runs"] += 1; b["mismatch"] += not ok
    out["f2_crash"] = crash
    out["f2_crash_pass"] = crash["mismatch"] == 0 and crash["runs"] == CR
    out["pass"] = bool(out["f1_f2_pass"] and out["f2_crash_pass"])
    evo.save("F1_F2_task_amend", out)
    print(json.dumps({k: out[k] for k in ("f2_crash", "f1_f2_pass", "f2_crash_pass", "pass")}, indent=1))


if __name__ == "__main__":
    main()
