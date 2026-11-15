"""F3: amendments arriving while work is in flight. 200 runs each, random injected delays 0-100 ms.
 race 1: a step is started, an amendment lands, the step completes (real concurrent appends to one session).
 race 2: a task-level concept fold is computing while an amendment lands (fence = compare-and-set on the view version).
 race 3: the ES projection of the task view is mid-batch while an amendment lands (fence = external version = view version).
usage: f3_inflight.py [runs=200]"""
import json, os, random, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import evo, task, taskview, scen
from memsys.esc import ES

T = "ev3"
INDEX = "sys-evo-task"


def setup_session(conn, sess, with_step=False):
    evs = [{"seq": 1, "kind": "task_defined", "trust_origin": "user_typed", "body": {"task_id": sess, "items": [
        {"id": "g", "type": "goal", "text": "goal text"}, {"id": "a", "type": "acceptance", "text": "acc v1"}, {"id": "s", "type": "scope", "text": "scope v1"}]}},
        {"seq": 2, "kind": "step_planned", "trust_origin": "assistant", "body": {"step_id": "p", "serves": ["a"]}}]
    taskview.append_many(conn, T, sess, evs)
    taskview.update_view(conn, T, sess)


def amend(conn, sess, tag):
    taskview.append(conn, T, sess, "task_amended", {"amendment_id": tag, "ops": [{"op": "change", "id": "a", "text": "acc " + tag}]}, n=500)


def thread_pair(fa, fb, delay_b):
    errs = []
    def w(f):
        def g():
            try: f()
            except Exception as e: errs.append(f"{type(e).__name__}: {str(e)[:100]}")
        return g
    ta, tb = threading.Thread(target=w(fa)), threading.Thread(target=w(fb))
    ta.start(); time.sleep(delay_b); tb.start(); ta.join(); tb.join()
    return errs


def race1(runs, rng):
    conn = evo.connect(); res = {"runs": runs, "torn": 0, "amend_before_start": 0, "amend_during": 0, "amend_after_completion": 0, "errors": 0}
    for i in range(runs):
        sess = f"r1-{i}"; setup_session(conn, sess)
        da, db_ = rng.uniform(0, .1), rng.uniform(0, .1)
        c1, c2 = evo.connect(), evo.connect()
        pre = rng.uniform(0, .1)
        def worker():
            time.sleep(pre)
            taskview.append(c1, T, sess, "step_started", {"step_id": "p"}, trust="assistant", n=10)
            time.sleep(da)
            taskview.append(c1, T, sess, "step_completed", {"step_id": "p", "artifacts": ["x"]}, trust="assistant", n=11)
        errs = thread_pair(worker, lambda: amend(c2, sess, "A1"), db_)
        res["errors"] += bool(errs)
        taskview.update_view(conn, T, sess)
        st, _ = taskview.read_view(conn, T, sess)
        evs = taskview.load_events(conn, T, sess)
        seq = {(e["kind"]): e["seq"] for e in evs if e["kind"] in ("step_started", "step_completed", "task_amended")}
        r = task.view(st)["steps"][0]
        a, s_, c_ = seq["task_amended"], seq["step_started"], seq["step_completed"]
        exp_basis = 2 if a < s_ else 1               # version in force when the step started, from log order
        exp_comp = "needs_recheck" if s_ < a < c_ else "still_valid"
        exp_now = "needs_recheck" if a > s_ else "still_valid"
        res["amend_before_start"] += a < s_; res["amend_during"] += s_ < a < c_; res["amend_after_completion"] += a > c_
        ok = (r["basis_version"] == exp_basis and r["class_at_completion"] == exp_comp and r["class_now"] == exp_now and r["artifacts"] == ["x"] and r["status"] == "completed"
              and r["completion_version"] == (2 if a < c_ else 1) and st["version"] == 2)
        res["torn"] += not ok
        c1.close(); c2.close()
    return res


def fold_once(conn, sess, fence, delay, stale_log):
    st, applied = taskview.read_view(conn, T, sess)
    dg = taskview.concept_digest(st); time.sleep(delay)
    with conn.transaction():
        lock = "for share" if fence else ""
        cur_applied = conn.execute(f"select applied_seq from task_view where tenant_id=%s and session_id=%s {lock}", (T, sess)).fetchone()[0]
        if fence and cur_applied != applied:
            return False
        stale_log.append(cur_applied != applied)             # did this commit land after the view had moved on?
        conn.execute("insert into task_concepts(tenant_id, session_id, built_seq, def_version, digest) values (%s,%s,%s,%s,%s) "
                     "on conflict (tenant_id, session_id) do update set built_seq=excluded.built_seq, def_version=excluded.def_version, digest=excluded.digest, committed_at=clock_timestamp()",
                     (T, sess, applied, st["version"], dg))
    return True


def race2(runs, rng, fence):
    conn = evo.connect(); res = {"runs": runs, "runs_with_stale_commit": 0, "rejected_stale_commits": 0, "runs_converged_after_refold": 0}
    for i in range(runs):
        sess = f"r2-{int(fence)}-{i}"; setup_session(conn, sess)
        da, db_ = rng.uniform(0, .1), rng.uniform(0, .1)
        c1, c2 = evo.connect(), evo.connect(); stale = []
        def folder():
            for attempt in range(6):
                if fold_once(c1, sess, fence, da if attempt == 0 else 0, stale): return
                res["rejected_stale_commits"] += 1
        def amender():
            amend(c2, sess, "A1"); taskview.update_view(c2, T, sess)
        thread_pair(folder, amender, db_)
        res["runs_with_stale_commit"] += any(stale)
        fold_once(conn, sess, True, 0, [])                  # the amendment marked the concept dirty: one more fenced fold converges
        st, applied = taskview.read_view(conn, T, sess)
        row = conn.execute("select built_seq, def_version, digest from task_concepts where tenant_id=%s and session_id=%s", (T, sess)).fetchone()
        res["runs_converged_after_refold"] += bool(row and row[0] == applied and row[1] == st["version"] and row[2] == taskview.concept_digest(st))
        c1.close(); c2.close()
    return res


def project_once(conn, es, sess, fence, delay):
    st, applied = taskview.read_view(conn, T, sess)
    time.sleep(delay)
    doc = {"task_id": sess, "applied_seq": applied, "def_version": st["version"], "digest": taskview.concept_digest(st)}
    if fence:
        es.req("PUT", f"/{INDEX}/_doc/{sess}", doc, ok=(200, 201, 409), version=applied, version_type="external_gte")
    else:
        es.req("PUT", f"/{INDEX}/_doc/{sess}", doc)


def race3(runs, rng, fence):
    conn = evo.connect(); es0 = ES(); res = {"runs": runs, "stale_in_es": 0}
    for i in range(runs):
        sess = f"r3-{int(fence)}-{i}"; setup_session(conn, sess)
        da, db_ = rng.uniform(0, .1), rng.uniform(0, .1)
        c1, c2 = evo.connect(), evo.connect(); e1, e2 = ES(), ES()
        def slow():
            project_once(c1, e1, sess, fence, da)
        def amender():
            amend(c2, sess, "A1"); taskview.update_view(c2, T, sess); project_once(c2, e2, sess, fence, 0)
        thread_pair(slow, amender, db_)
        st, applied = taskview.read_view(conn, T, sess)
        d = es0.req("GET", f"/{INDEX}/_doc/{sess}")[1]["_source"]
        res["stale_in_es"] += d["applied_seq"] != applied or d["def_version"] != st["version"]
        c1.close(); c2.close()
    return res


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rng = random.Random(16096)
    conn = evo.connect(); evo.reset(conn); taskview.setup(conn)
    es = ES(); es.req("DELETE", f"/{INDEX}", ok=None)
    out = {"workload": f"{runs} runs per race and mode, random delays 0-100 ms on both sides; PG16 + ES 9.5.6 on t2-dev", "results": {}}
    out["results"]["race1_step_started_amend_completed"] = race1(runs, rng); print(out["results"], flush=True)
    for name, fn in (("race2_fold", race2), ("race3_projection", race3)):
        for fence in (True, False):
            out["results"][f"{name}_fence_{'on' if fence else 'off'}"] = fn(runs, rng, fence); print(name, fence, out["results"][f"{name}_fence_{'on' if fence else 'off'}"], flush=True)
    R = out["results"]
    out["pass"] = (R["race1_step_started_amend_completed"]["torn"] == 0 and R["race1_step_started_amend_completed"]["errors"] == 0 and R["race2_fold_fence_on"]["runs_with_stale_commit"] == 0 and R["race2_fold_fence_on"]["runs_converged_after_refold"] == runs
                   and R["race3_projection_fence_on"]["stale_in_es"] == 0)
    out["controls_sensitive"] = {"fold": R["race2_fold_fence_off"]["runs_with_stale_commit"] > 0, "projection": R["race3_projection_fence_off"]["stale_in_es"] > 0}
    evo.save("F3_inflight", out)
    print("PASS" if out["pass"] else "FAIL", out["controls_sensitive"])


if __name__ == "__main__":
    main()
