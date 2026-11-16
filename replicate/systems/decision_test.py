"""DESIGN 3.5.1, 3.12.1, 3.5.2 and 4.2 row 2 (THRESHOLDS.md section D). Owner evolution/task.py, evolution/taskview.py
and memsys/conceptq.py at a13b524 imported read-only; scenario generator, oracle, race drivers and observers are mine.
evolution/oracle.py, evolution/scen.py and evolution/f*.py are not imported.

Oracle rule (from task.py's docstring and DESIGN 3.5.1, written independently): a definition version changes only on a
user_typed task_defined (first only) or a user_typed task_amended with a new id and at least one effective op. A step's
basis is the version in force when it started (else when it completed). Now: every served item removed -> obsolete;
any served item whose text differs from the basis (or is absent from the basis) -> needs_recheck; else still_valid.
A step whose served items are only PARTLY removed (others unchanged) is not defined by the rule text: it is counted as
`ambiguous` and excluded from the comparison (reported, with the owner's choice).

usage (systems venv): python decision_test.py [scenarios=600] [pg_scenarios=150] [runs=200]
"""
import copy
import json
import multiprocessing as mp
import os
import random
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysdrv  # noqa: E402
from memsys import conceptq, db  # noqa: E402
from evolution import task, taskview  # noqa: E402

T = "repdec"
TYPES = ("goal", "acceptance", "scope", "constraint")


# ------------------------------------------------------------------ generator (mine)
def gen_scenario(rng, sid):
    """Events with seq 1..n. Returns events and per-event notes."""
    evs, seq = [], [0]

    def add(kind, trust, body):
        seq[0] += 1
        evs.append({"seq": seq[0], "kind": kind, "trust_origin": trust, "body": body})

    n_items = rng.randint(3, 12)
    if rng.random() < 0.2:
        add("task_defined", "web", {"task_id": sid, "items": [{"id": "x0", "type": "goal", "text": "injected"}]})
    items = {"i%d" % i: {"type": rng.choice(TYPES), "text": "t%d-v0" % i} for i in range(n_items)}
    add("task_defined", "user_typed", {"task_id": sid, "items": [dict(id=k, **v) for k, v in items.items()]})
    live = dict(items)
    next_item, steps, amend_n, history = n_items, [], 0, {k: [v["text"]] for k, v in items.items()}
    for _ in range(rng.randint(8, 60)):
        r = rng.random()
        if r < 0.30 and live:
            sid_ = "s%d" % len(steps)
            steps.append({"id": sid_, "state": "planned"})
            add("step_planned", "assistant", {"step_id": sid_, "serves": rng.sample(sorted(live), rng.randint(0, min(3, len(live))))})
        elif r < 0.50 and steps:
            st = rng.choice(steps)
            if st["state"] == "planned" and rng.random() < 0.8:
                st["state"] = "in_flight"
                add("step_started", "assistant", {"step_id": st["id"]})
            elif st["state"] in ("planned", "in_flight"):
                st["state"] = "completed"
                add("step_completed", "assistant", {"step_id": st["id"], "artifacts": ["a-%s" % st["id"]]})
        elif r < 0.53 and steps:
            add("step_invalidated", "assistant", {"step_id": rng.choice(steps)["id"], "reason": "manual"})
        elif r < 0.75:
            ops = []
            for _ in range(rng.randint(1, 3)):
                o = rng.random()
                if o < 0.25:
                    nid = "i%d" % next_item
                    next_item += 1
                    ops.append({"op": "add", "item": {"id": nid, "type": rng.choice(TYPES), "text": "%s-v0" % nid}})
                elif o < 0.45 and live:
                    ops.append({"op": "remove", "id": rng.choice(sorted(live))})
                elif o < 0.60 and live:     # revert a changed item to an earlier text
                    k = rng.choice(sorted(live))
                    ops.append({"op": "change", "id": k, "text": history[k][0]})
                elif o < 0.70:
                    ops.append({"op": "remove", "id": "nope%d" % rng.randint(0, 9)})
                elif live:
                    k = rng.choice(sorted(live))
                    ops.append({"op": "change", "id": k, "text": "%s-v%d" % (k, rng.randint(1, 9))})
            trust = "user_typed" if rng.random() < 0.8 else rng.choice(["assistant", "tool", "web"])
            aid = "a%d" % amend_n if rng.random() > 0.05 or amend_n == 0 else "a%d" % (amend_n - 1)   # 5% duplicate ids
            amend_n += 1
            add("task_amended", trust, {"amendment_id": aid, "ops": ops})
            # keep the generator's view of live items roughly in step (the oracle recomputes exactly)
            if trust == "user_typed":
                for op in ops:
                    if op["op"] == "add":
                        live[op["item"]["id"]] = op["item"]
                        history.setdefault(op["item"]["id"], [op["item"]["text"]])
                    elif op["op"] == "remove":
                        live.pop(op["id"], None)
                    elif op["op"] == "change" and op["id"] in live:
                        history[op["id"]].append(op["text"])
    return evs


# ------------------------------------------------------------------ oracle (mine)
def oracle(evs):
    ver, items, defs, seen_ids, defined = 0, {}, {}, set(), False
    steps, amends, net_zero = {}, [], []
    for e in sorted(evs, key=lambda e: e["seq"]):
        k, b, tr = e["kind"], e["body"], e["trust_origin"]
        if k == "task_defined":
            if tr == "user_typed" and not defined:
                defined, ver = True, 1
                items = {i["id"]: (i["type"], i["text"]) for i in b["items"]}
                defs[1] = dict(items)
                amends.append({"seq": e["seq"], "version": 1, "prev": {}, "now": dict(items)})
        elif k == "task_amended":
            if tr != "user_typed" or not defined or b["amendment_id"] in seen_ids:
                continue
            new, effective = dict(items), 0
            for op in b["ops"]:
                if op["op"] == "add" and op["item"]["id"] not in new:
                    new[op["item"]["id"]] = (op["item"]["type"], op["item"]["text"])
                    effective += 1
                elif op["op"] == "remove" and op["id"] in new:
                    new.pop(op["id"])
                    effective += 1
                elif op["op"] == "change" and op["id"] in new and new[op["id"]][1] != op["text"]:
                    new[op["id"]] = (new[op["id"]][0], op["text"])
                    effective += 1
            # Run 1 (d77bd1d) compared the net definition (new != items) instead of counting effective ops, contrary
            # to the rule stated above; it disagreed with the owner on a net-zero amendment (change X then change X
            # back in one amendment). Fixed to the stated rule; net-zero amendments are counted as a finding.
            if effective:
                if new == items:
                    net_zero.append(e["seq"])
                seen_ids.add(b["amendment_id"])
                ver += 1
                amends.append({"seq": e["seq"], "version": ver, "prev": dict(items), "now": dict(new)})
                items = new
                defs[ver] = dict(items)
        else:
            st = steps.setdefault(b["step_id"], {"serves": [], "basis": None, "inval": False, "comp": None})
            if k == "step_planned":
                st["serves"] = list(b.get("serves", []))
            elif k == "step_started":
                st["basis"] = ver
            elif k == "step_completed":
                st["comp"] = ver
                if st["basis"] is None:
                    st["basis"] = ver
            else:
                st["inval"] = True
    out = {}
    for sid, st in steps.items():
        if st["inval"]:
            out[sid] = "invalidated"
        elif st["basis"] is None:
            out[sid] = None
        else:
            base, sv = defs[st["basis"]], st["serves"]
            gone = [i for i in sv if i not in items]
            kept = [i for i in sv if i in items]
            changed = [i for i in kept if i not in base or base[i][1] != items[i][1]]
            if sv and not kept:
                out[sid] = "obsolete"
            elif changed:
                out[sid] = "needs_recheck"
            elif gone:
                out[sid] = "ambiguous"
            else:
                out[sid] = "still_valid"
    return {"version": ver, "items": {k: v[1] for k, v in items.items()}, "classes": out, "amends": amends, "net_zero": net_zero}


def compare(o, v):
    """Disagreements between my oracle and an owner view (dict from task.view / task_state)."""
    d, amb, amb_owner = [], 0, {}
    if o["version"] != v["version"]:
        d.append(("version", o["version"], v["version"]))
    if o["items"] != {k: x["text"] for k, x in v["items"].items()}:
        d.append(("items", None, None))
    got = {r["step_id"]: r["class_now"] for r in v["steps"]}
    for sid, c in o["classes"].items():
        if c == "ambiguous":
            amb += 1
            amb_owner[got.get(sid)] = amb_owner.get(got.get(sid), 0) + 1
        elif got.get(sid) != c:
            d.append(("step", sid, c, got.get(sid)))
    return d, amb, amb_owner


def check_render(evs, o):
    """3.12.1: after each effective amendment, the rendered next-turn block shows the new definition as [CURRENT] and
    the old text of every changed/removed item as [SUPERSEDED]; the old text is never shown as [CURRENT]."""
    bad, checked = [], 0
    for a in o["amends"]:
        if a["version"] < 2:
            continue
        txt = task.render(task.fold([e for e in evs if e["seq"] <= a["seq"]]))
        cur = [ln for ln in txt.splitlines() if ln.startswith("[CURRENT]")]
        sup = [ln for ln in txt.splitlines() if ln.startswith("[SUPERSEDED]")]
        checked += 1
        for iid, (typ, text) in a["now"].items():
            if "[CURRENT] %s (%s): %s" % (iid, typ, text) not in cur:
                bad.append((a["seq"], iid, "current line missing"))
        for iid, (typ, text) in a["prev"].items():
            if iid not in a["now"] or a["now"][iid][1] != text:
                if not any(ln.startswith("[SUPERSEDED] %s " % iid) and text in ln for ln in sup):
                    bad.append((a["seq"], iid, "superseded line missing"))
                if any(ln == "[CURRENT] %s (%s): %s" % (iid, typ, text) for ln in cur):
                    bad.append((a["seq"], iid, "old text shown as current"))
    return checked, bad


def part_f1(n, rng):
    res = {"scenarios": n, "steps_compared": 0, "steps_ambiguous_partial_removal": 0, "owner_class_for_ambiguous": {},
           "scenarios_with_disagreement": 0, "disagreement_examples": [], "renders_checked": 0, "render_failures": 0,
           "render_examples": [], "low_trust_amendments": 0, "effective_amendments": 0,
           "class_counts": {}, "controls": {}}
    c1 = c2 = c3 = 0
    scen = []
    for i in range(n):
        evs = gen_scenario(rng, "sc%d" % i)
        scen.append(evs)
        o = oracle(evs)
        v = task.task_state(evs)
        d, amb, ao = compare(o, v)
        res["steps_compared"] += sum(1 for c in o["classes"].values() if c != "ambiguous")
        res["steps_ambiguous_partial_removal"] += amb
        for k, x in ao.items():
            res["owner_class_for_ambiguous"][str(k)] = res["owner_class_for_ambiguous"].get(str(k), 0) + x
        for c in o["classes"].values():
            res["class_counts"][str(c)] = res["class_counts"].get(str(c), 0) + 1
        res["low_trust_amendments"] += sum(1 for e in evs if e["kind"] == "task_amended" and e["trust_origin"] != "user_typed")
        res["effective_amendments"] += len(o["amends"]) - 1
        res["net_zero_amendments_new_version_same_definition"] = res.get("net_zero_amendments_new_version_same_definition", 0) + len(o["net_zero"])
        if d:
            res["scenarios_with_disagreement"] += 1
            if len(res["disagreement_examples"]) < 5:
                res["disagreement_examples"].append({"scenario": i, "diff": d[:5]})
        ck, bad = check_render(evs, o)
        res["renders_checked"] += ck
        res["render_failures"] += len(bad)
        if bad and len(res["render_examples"]) < 5:
            res["render_examples"].append({"scenario": i, "bad": bad[:3]})
        # c1: every step still valid
        v1 = copy.deepcopy(v)
        for r in v1["steps"]:
            if r["class_now"] not in (None, "invalidated"):
                r["class_now"] = "still_valid"
        c1 += bool(compare(o, v1)[0])
        # c2: a view that ignores amendment events
        c2 += bool(compare(o, task.task_state([e for e in evs if e["kind"] != "task_amended"]))[0])
        # c3 (render sensitivity): drop the SUPERSEDED lines
        if any(a["version"] >= 2 for a in o["amends"]):
            a = [a for a in o["amends"] if a["version"] >= 2][-1]
            txt = task.render(task.fold([e for e in evs if e["seq"] <= a["seq"]]))
            stripped = "\n".join(ln for ln in txt.splitlines() if not ln.startswith("[SUPERSEDED]"))
            sup_needed = any(iid not in a["now"] or a["now"][iid][1] != t for iid, (_, t) in a["prev"].items())
            c3 += sup_needed and "[SUPERSEDED]" not in stripped
    res["controls"] = {"c1_all_still_valid_scenarios_flagged": c1, "c2_ignore_amendments_scenarios_flagged": c2,
                       "c3_superseded_stripped_flagged": c3,
                       "c1_ge_10pct": c1 >= 0.1 * n, "c2_ge_10pct": c2 >= 0.1 * n}
    return res, scen


def part_pg(conn, scen, m):
    """Same scenarios through the owner's Postgres path: append to the log, materialise task_view in two catch-ups."""
    taskview.setup(conn)
    res = {"scenarios": m, "mismatch_vs_pure_fold": 0, "mismatch_vs_oracle": 0, "examples": []}
    for i, evs in enumerate(scen[:m]):
        sess = "pg%d" % i
        cut = len(evs) // 2
        taskview.append_many(conn, T, sess, evs[:cut])
        taskview.update_view(conn, T, sess)
        taskview.append_many(conn, T, sess, evs[cut:])
        taskview.update_view(conn, T, sess)
        st, _ = taskview.read_view(conn, T, sess)
        v = task.view(st)
        pure = task.task_state(evs)
        same = (v["version"] == pure["version"] and v["items"] == pure["items"]
                and [(r["step_id"], r["class_now"], r["basis_version"]) for r in v["steps"]] == [(r["step_id"], r["class_now"], r["basis_version"]) for r in pure["steps"]])
        res["mismatch_vs_pure_fold"] += not same
        d = compare(oracle(evs), v)[0]
        res["mismatch_vs_oracle"] += bool(d)
        if (not same or d) and len(res["examples"]) < 3:
            res["examples"].append({"i": i, "diff": d[:3]})
    return res


# ------------------------------------------------------------------ 3.5.2 races (my drivers)
def setup_session(conn, sess):
    evs = [{"seq": 1, "kind": "task_defined", "trust_origin": "user_typed", "body": {"task_id": sess, "items": [
        {"id": "g", "type": "goal", "text": "goal"}, {"id": "a", "type": "acceptance", "text": "acc v1"}]}},
        {"seq": 2, "kind": "step_planned", "trust_origin": "assistant", "body": {"step_id": "p", "serves": ["a"]}}]
    taskview.append_many(conn, T, sess, evs)
    taskview.update_view(conn, T, sess)


def amend(conn, sess):
    taskview.append(conn, T, sess, "task_amended", {"amendment_id": "A1", "ops": [{"op": "change", "id": "a", "text": "acc v2"}]})


def pair(fa, fb, delay_b):
    errs = []

    def w(f):
        def g():
            try:
                f()
            except Exception as e:  # noqa: BLE001
                errs.append("%s: %s" % (type(e).__name__, str(e)[:120]))
        return g
    ta, tb = threading.Thread(target=w(fa)), threading.Thread(target=w(fb))
    ta.start()
    time.sleep(delay_b)
    tb.start()
    ta.join()
    tb.join()
    return errs


def race1(runs, rng):
    """Step started, amendment, step completed (real concurrent appends). Truth: my oracle over the log order.
    Control (stale token): the basis a worker would record from its own read of the task before starting."""
    conn = db.connect(autocommit=True)
    r = {"runs": runs, "torn": 0, "overlap_witness_amend_between_start_and_complete": 0, "amend_before_start": 0,
         "amend_after_complete": 0, "errors": 0, "control_stale_read_basis_wrong": 0, "examples": []}
    for i in range(runs):
        sess = "r1-%d" % i
        setup_session(conn, sess)
        c1, c2 = db.connect(autocommit=True), db.connect(autocommit=True)
        think, gap, d_b = rng.uniform(0, .1), rng.uniform(0, .1), rng.uniform(0, .1)
        seen = {}

        def worker():
            seen["ver"] = oracle(taskview.load_events(c1, T, sess))["version"]   # the worker reads the task
            time.sleep(think)
            taskview.append(c1, T, sess, "step_started", {"step_id": "p"}, trust="assistant")
            time.sleep(gap)
            taskview.append(c1, T, sess, "step_completed", {"step_id": "p", "artifacts": ["x"]}, trust="assistant")
        errs = pair(worker, lambda: amend(c2, sess), d_b)
        r["errors"] += bool(errs)
        taskview.update_view(conn, T, sess)
        st, _ = taskview.read_view(conn, T, sess)
        evs = taskview.load_events(conn, T, sess)
        o = oracle(evs)
        sq = {e["kind"]: e["seq"] for e in evs}
        a, s_, c_ = sq["task_amended"], sq["step_started"], sq["step_completed"]
        r["overlap_witness_amend_between_start_and_complete"] += s_ < a < c_
        r["amend_before_start"] += a < s_
        r["amend_after_complete"] += a > c_
        row = task.view(st)["steps"][0]
        true_basis = 2 if a < s_ else 1
        ok = (row["class_now"] == o["classes"]["p"] and row["basis_version"] == true_basis and row["status"] == "completed"
              and row["artifacts"] == ["x"] and st["version"] == 2)
        r["torn"] += not ok
        r["control_stale_read_basis_wrong"] += seen.get("ver") != true_basis
        if not ok and len(r["examples"]) < 3:
            r["examples"].append({"i": i, "row": row, "oracle": o["classes"]["p"], "true_basis": true_basis})
        c1.close()
        c2.close()
    return r


AUDIT = """
create table if not exists rep_audit(session_id text, built_seq bigint, view_seq bigint, at timestamptz default clock_timestamp());
create or replace function rep_audit_f() returns trigger language plpgsql as $$ begin
  insert into rep_audit(session_id, built_seq, view_seq) select NEW.session_id, NEW.built_seq,
    (select applied_seq from task_view v where v.tenant_id = NEW.tenant_id and v.session_id = NEW.session_id);
  return NEW; end $$;
drop trigger if exists rep_audit_t on task_concepts;
create trigger rep_audit_t after insert or update on task_concepts for each row execute function rep_audit_f();
"""


def fold_once(conn, sess, fence, delay):
    """The design's fold fence (compare-and-set on the view cursor under FOR SHARE), written here because the owner
    library has no fenced fold function (the fence exists only in exp script evolution/f3_inflight.py)."""
    st, applied = taskview.read_view(conn, T, sess)
    dg = taskview.concept_digest(st)
    time.sleep(delay)
    with conn.transaction():
        if fence:
            cur = conn.execute("select applied_seq from task_view where tenant_id=%s and session_id=%s for share", (T, sess)).fetchone()[0]
            if cur != applied:
                return False
        conn.execute("insert into task_concepts(tenant_id, session_id, built_seq, def_version, digest) values (%s,%s,%s,%s,%s) "
                     "on conflict (tenant_id, session_id) do update set built_seq=excluded.built_seq, def_version=excluded.def_version, "
                     "digest=excluded.digest, committed_at=clock_timestamp()", (T, sess, applied, st["version"], dg))
    return True


def race2(runs, rng, fence):
    conn = db.connect(autocommit=True)
    conn.execute(AUDIT)
    r = {"runs": runs, "fence": fence, "runs_with_stale_commit_observed_by_trigger": 0, "rejected_attempts_overlap_witness": 0,
         "converged_after_refold": 0, "errors": 0}
    for i in range(runs):
        sess = "r2-%d-%d" % (fence, i)
        setup_session(conn, sess)
        c1, c2 = db.connect(autocommit=True), db.connect(autocommit=True)
        da, db_ = rng.uniform(0, .1), rng.uniform(0, .1)

        def folder():
            for att in range(6):
                if fold_once(c1, sess, fence, da if att == 0 else 0):
                    return
                r["rejected_attempts_overlap_witness"] += 1

        def amender():
            amend(c2, sess)
            taskview.update_view(c2, T, sess)
        r["errors"] += bool(pair(folder, amender, db_))
        stale = conn.execute("select count(*) from rep_audit where session_id=%s and built_seq < view_seq", (sess,)).fetchone()[0]
        r["runs_with_stale_commit_observed_by_trigger"] += stale > 0
        fold_once(conn, sess, True, 0)
        st, applied = taskview.read_view(conn, T, sess)
        row = conn.execute("select built_seq, def_version, digest from task_concepts where tenant_id=%s and session_id=%s", (T, sess)).fetchone()
        r["converged_after_refold"] += bool(row and row[0] == applied and row[1] == st["version"] and row[2] == taskview.concept_digest(st))
        c1.close()
        c2.close()
    return r


def race3(runs, rng, fence, idx="rep-decision-task"):
    conn = db.connect(autocommit=True)
    r = {"runs": runs, "fence": fence, "stale_in_es": 0, "errors": 0}
    for i in range(runs):
        sess = "r3-%d-%d" % (fence, i)
        setup_session(conn, sess)
        c1, c2, e1, e2, e0 = db.connect(autocommit=True), db.connect(autocommit=True), sysdrv.RepES(), sysdrv.RepES(), sysdrv.RepES()
        da, db_ = rng.uniform(0, .1), rng.uniform(0, .1)

        def project(c, e, delay):
            st, applied = taskview.read_view(c, T, sess)
            time.sleep(delay)
            doc = {"applied_seq": applied, "def_version": st["version"]}
            if fence:
                e.req("PUT", "/%s/_doc/%s" % (idx, sess), doc, ok=(200, 201, 409), version=applied, version_type="external_gte")
            else:
                e.req("PUT", "/%s/_doc/%s" % (idx, sess), doc)

        def amender():
            amend(c2, sess)
            taskview.update_view(c2, T, sess)
            project(c2, e2, 0)
        r["errors"] += bool(pair(lambda: project(c1, e1, da), amender, db_))
        st, applied = taskview.read_view(conn, T, sess)
        d = e0.req("GET", "/%s/_doc/%s" % (idx, sess))[1]["_source"]
        r["stale_in_es"] += d["applied_seq"] != applied or d["def_version"] != st["version"]
        c1.close()
        c2.close()
    return r


# ------------------------------------------------------------------ 4.2 concept queue (owner conceptq, my workers)
def naive_claim(conn):
    rows = conn.execute("select tenant_id, concept_id, gen from concepts where dirty and next_fold_at <= clock_timestamp() "
                        "order by next_fold_at limit 1").fetchall()
    return [(t, c, "naive-" + uuid.uuid4().hex, g) for t, c, g in rows]


def cq_worker(wid, stop, mode, work_s):
    conn, log = db.connect(autocommit=True), db.connect(autocommit=True)
    while not stop.is_set():
        rows = conceptq.claim(conn, limit=1) if mode == "queue" else naive_claim(conn)
        if not rows:
            time.sleep(0.01)
            continue
        for t, c, tok, g in rows:
            t0 = time.time()
            time.sleep(work_s)
            res = conceptq.commit_fold(conn, t, c, tok, g, ([], [], "d-" + tok[:8]), fence=(mode == "queue"))
            log.execute("insert into rep_foldlog values (%s,%s,%s,%s,%s,%s,%s)", (mode, wid, c, tok, t0, time.time(), res))


def cq_run(mode, seconds, rate, n_concepts, workers, rng):
    conn = db.connect(autocommit=True)
    conn.execute("delete from concepts where tenant_id=%s", (T,))
    stop = mp.Event()
    ps = [mp.Process(target=cq_worker, args=(w, stop, mode, 0.02)) for w in range(workers)]
    for p in ps:
        p.start()
    weights = [1.0 / (k + 1) ** 1.1 for k in range(n_concepts)]
    acts, t0 = 0, time.time()
    while time.time() - t0 < seconds:
        conceptq.touch_concepts(conn, T, ["k%04d" % rng.choices(range(n_concepts), weights)[0]])
        acts += 1
        time.sleep(1.0 / rate)
    t1 = time.time()
    while conn.execute("select count(*) from concepts where tenant_id=%s and dirty", (T,)).fetchone()[0] and time.time() - t1 < 30:
        time.sleep(0.2)
    stop.set()
    for p in ps:
        p.join(30)
    rows = conn.execute("select concept_id, t0, t1, result from rep_foldlog where mode=%s order by concept_id, t0", (mode,)).fetchall()
    by = {}
    for c, a, b, res in rows:
        by.setdefault(c, []).append((a, b, res))
    hot = ["k%04d" % k for k in range(5)]
    overl, hot_iv, hot_ov = 0, 0, 0
    for c, iv in by.items():
        mx = -1.0
        for a, b, _ in iv:
            o = a < mx
            overl += o
            if c in hot:
                hot_iv += 1
                hot_ov += o
            mx = max(mx, b)
    still = conn.execute("select count(*) from concepts where tenant_id=%s and dirty", (T,)).fetchone()[0]
    return {"mode": mode, "seconds": seconds, "activities": acts, "workers": workers, "concepts": n_concepts,
            "fold_attempts": len(rows), "results": {k: sum(1 for *_, r in rows if r == k) for k in ("committed", "committed_dirty", "rejected")},
            "overlapping_fold_intervals_same_concept": overl, "hot_concept_intervals": hot_iv, "hot_concept_overlapping": hot_ov,
            "hot_overlap_fraction": round(hot_ov / hot_iv, 4) if hot_iv else None, "still_dirty_after_drain": still}


def lease_case(fence):
    conn = db.connect(autocommit=True)
    cid = "lease-%d" % fence
    conceptq.touch_concepts(conn, T, [cid], debounce=0, max_wait=0)
    time.sleep(0.05)
    a = [r for r in conceptq.claim(conn, limit=50, lease=0.5) if r[1] == cid]
    time.sleep(1.0)
    b = [r for r in conceptq.claim(conn, limit=50, lease=30) if r[1] == cid]
    out = {"fence": fence, "a_claimed": bool(a), "b_claimed_after_expiry": bool(b)}
    if a and b:
        out["b_commit"] = conceptq.commit_fold(conn, T, cid, b[0][2], b[0][3], ([], [], "B"), fence=fence)
        out["a_late_commit"] = conceptq.commit_fold(conn, T, cid, a[0][2], a[0][3], ([], [], "A"), fence=fence)
        out["final_digest"] = conn.execute("select digest from concepts where tenant_id=%s and concept_id=%s", (T, cid)).fetchone()[0]
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    m = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    runs = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    rng = random.Random(16096)
    conn, _, es = sysdrv.conns()
    la0 = os.getloadavg()
    db.reset_schema(conn)
    out = {}
    out["F1_3_5_1_and_3_12_1"], scen = part_f1(n, rng)
    print(json.dumps({k: v for k, v in out["F1_3_5_1_and_3_12_1"].items() if "examples" not in k}), flush=True)
    out["F1_pg_path"] = part_pg(conn, scen, m)
    print(out["F1_pg_path"], flush=True)
    es.req("DELETE", "/rep-decision-task", ok=None)
    out["F3_race1"] = race1(runs, rng)
    print(out["F3_race1"], flush=True)
    for fence in (True, False):
        out["F3_race2_fence_%s" % ("on" if fence else "off")] = race2(runs, rng, fence)
        print(out["F3_race2_fence_%s" % ("on" if fence else "off")], flush=True)
        out["F3_race3_fence_%s" % ("on" if fence else "off")] = race3(runs, rng, fence)
        print(out["F3_race3_fence_%s" % ("on" if fence else "off")], flush=True)
    conn.execute("create table if not exists rep_foldlog(mode text, wid int, concept_id text, token text, t0 float8, t1 float8, result text)")
    out["C8_queue"] = cq_run("queue", 30, 150, 300, 8, rng)
    print(out["C8_queue"], flush=True)
    out["C8_control_no_lease"] = cq_run("naive", 30, 150, 300, 8, rng)
    print(out["C8_control_no_lease"], flush=True)
    out["C8_lease_expiry_fenced"] = lease_case(True)
    out["C8_lease_expiry_control_unfenced"] = lease_case(False)
    print(out["C8_lease_expiry_fenced"], out["C8_lease_expiry_control_unfenced"], flush=True)
    F, P, R1 = out["F1_3_5_1_and_3_12_1"], out["F1_pg_path"], out["F3_race1"]
    R2on, R2off, R3on, R3off = (out["F3_race2_fence_on"], out["F3_race2_fence_off"], out["F3_race3_fence_on"], out["F3_race3_fence_off"])
    Q, QC, L = out["C8_queue"], out["C8_control_no_lease"], out["C8_lease_expiry_fenced"]
    out["threshold_checks"] = {
        "3.5.1_zero_disagreements": F["scenarios_with_disagreement"] == 0 and P["mismatch_vs_oracle"] == 0 and P["mismatch_vs_pure_fold"] == 0,
        "3.12.1_render_zero_failures": F["render_failures"] == 0 and F["renders_checked"] > 0,
        "controls_c1_c2_ge_10pct": F["controls"]["c1_ge_10pct"] and F["controls"]["c2_ge_10pct"],
        "3.5.2_race1_torn_0": R1["torn"] == 0 and R1["errors"] == 0,
        "3.5.2_race1_overlap_witness_gt_0": R1["overlap_witness_amend_between_start_and_complete"] > 0,
        "3.5.2_race1_control_ge_10pct": R1["control_stale_read_basis_wrong"] >= 0.1 * runs,
        "3.5.2_race2_fence_on_stale_0": R2on["runs_with_stale_commit_observed_by_trigger"] == 0 and R2on["converged_after_refold"] == runs,
        "3.5.2_race2_overlap_witness_gt_0": R2on["rejected_attempts_overlap_witness"] > 0,
        "3.5.2_race2_fence_off_ge_10pct": R2off["runs_with_stale_commit_observed_by_trigger"] >= 0.1 * runs,
        "3.5.2_race3_fence_on_stale_0": R3on["stale_in_es"] == 0,
        "3.5.2_race3_fence_off_ge_10pct": R3off["stale_in_es"] >= 0.1 * runs,
        "4.2_queue_overlap_0": Q["overlapping_fold_intervals_same_concept"] == 0 and Q["still_dirty_after_drain"] == 0,
        "4.2_control_hot_overlap_ge_10pct": (QC["hot_overlap_fraction"] or 0) >= 0.10,
        "4.2_lease_expiry_late_commit_rejected": L.get("a_late_commit") == "rejected" and L.get("final_digest") == "B",
        "4.2_lease_control_unfenced_accepts_late": out["C8_lease_expiry_control_unfenced"].get("a_late_commit") != "rejected"}
    out = {"stamp": sysdrv.stamp(__file__, {"scenarios": n, "pg_scenarios": m, "runs": runs, "seed": 16096, "cq_seconds": 30,
                                            "cq_rate_per_s": 150, "cq_concepts": 300, "cq_workers": 8, "fold_work_s": 0.02}, conn, es),
           "loadavg_before": la0, "loadavg_after": os.getloadavg(),
           "owner": {"F1": "2000 scenarios, failures {} (F1_F2_task_amend.json a13b524)", "F3": "race1 torn 0; fence-off 74 and 71 of 200",
                     "C8": "overlapping 0; lease-expiry late commit rejected"}, **out}
    p = sysdrv.save("decision_3_5_x_4_2.json", out)
    print(out["threshold_checks"], "saved", p)


if __name__ == "__main__":
    main()
