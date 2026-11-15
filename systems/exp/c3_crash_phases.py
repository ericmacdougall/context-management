"""C3 crash between phases: a failure or process kill after phase 1 must leave a durable job, hide the erased data from
readers while it is pending, and be completed by the sweeper (bytes gone from the ES data dir).
Modes: injected exception at 4 points (10 runs each), SIGKILL of the erasing child at 4 points (10 each), and ES errors
injected at a random request during the purge (20 runs). usage: c3_crash_phases.py [per_point=10] [es_error_runs=20]"""
import random, subprocess, sys, time, uuid, datetime as dt
from common import *
from c3_erasure_v2 import grep, ES_C, ES_ROOT
from memsys import store, projector, erasure, reader, kms
from memsys.esc import ES, ESError

T = "cp"
PY = sys.executable
POINTS = ["after_phase1", "before_key_destroy", "before_es_purge", "after_es_purge"]
TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


class FlakyES(ES):
    def __init__(self, fail_at):
        super().__init__(); self.n = 0; self.fail_at = fail_at

    def req(self, *a, **k):
        self.n += 1
        if self.n == self.fail_at:
            raise ESError(503, {"error": "injected ES failure"}, "injected")
        return super().req(*a, **k)


def setup_victim(conn, lock, es, r):
    subj, m = f"v{r}", "zxq" + uuid.uuid4().hex[:12]
    ev = lambda i, text, s: dict(ts_event=TS + dt.timedelta(minutes=r * 10 + i), actor="user", trust_origin="user_typed", kind="message", subject_id=s, content=text)
    store.append_events(conn, T, f"s{r}", [ev(0, f"Vic{r}'s hobby{m} is value{m}", subj), ev(1, f"note {m}", subj)])
    store.append_events(conn, T, f"k{r}", [ev(2, f"Keeper{r}'s city is Oslo", "keeper")])
    projector.Projector(conn, lock, es, recipes=(1, 2), live_recipe=1, tenants=[T]).run()
    return subj, m


def check_pending(conn, es, rd, subj, m):
    ids = [x[0] for x in conn.execute("select event_id from tombstones where tenant_id=%s and subject_id=%s", (T, subj)).fetchall()]
    raw = es.req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}})[1]["hits"]["total"]["value"]
    hits = {h["id"] for h in rd.search(T, m, k=10, tail=False)["hits"]}
    fx = [r["index"] for r in esx.tenant_indices(es, T) if "-facts-v" in r["index"]]
    victim_facts = {h["_id"] for h in es.req("POST", f"/{','.join(fx)}/_search", {"size": 100, "_source": False, "query": {"terms": {"input_event_ids": ids}}})[1]["hits"]["hits"]}
    fh = rd.search_facts(T, f"value{m}", k=50)
    return {"es_raw_docs_still_present": raw + len(victim_facts), "reader_returned_erased_events": len(hits & set(ids)), "reader_fact_hits": len(set(fh["hits"]) & victim_facts)}


def finish(conn, lock, es, subj, m, res, mode, point):
    job = conn.execute("select job_id, state from erasure_jobs where tenant_id=%s and subject_id=%s", (T, subj)).fetchone()
    res["job_row_present"] += job is not None
    pending = job and job[1] != "done"
    res["left_pending"] += bool(pending)
    if pending:
        rd = reader.Reader(conn, es)
        p = check_pending(conn, es, rd, subj, m)
        res["raw_es_had_erased_docs_while_pending"] += p["es_raw_docs_still_present"] > 0
        res["reader_leaks_while_pending"] += p["reader_returned_erased_events"] > 0 or p["reader_fact_hits"] > 0
        out = erasure.sweep(conn, lock, es)
        res["sweeper_completed"] += out.get(job[0]) == "done"
    done = conn.execute("select state from erasure_jobs where tenant_id=%s and subject_id=%s", (T, subj)).fetchone()
    ids = [x[0] for x in conn.execute("select event_id from tombstones where tenant_id=%s and subject_id=%s", (T, subj)).fetchall()]
    bad = (erasure.verify(conn, kms.get(), es, {"tenant_id": T, "subject_id": subj, "event_ids": ids}))
    clean = done and done[0] == "done" and bad["es_event_docs"] == 0 and bad["es_fact_docs_touching"] == 0 and bad["pg_facts_touching_tombstoned"] == 0 and bad["events_still_decryptable"] == 0 and bad["key_destroyed"]
    marker_files = grep(ES_C, ES_ROOT, m)["files"]
    res["final_clean"] += bool(clean) and marker_files == 0
    res["marker_files_after_sweep_nonzero"] += marker_files > 0
    res["runs"] += 1


def blank():
    return {"runs": 0, "job_row_present": 0, "left_pending": 0, "raw_es_had_erased_docs_while_pending": 0, "reader_leaks_while_pending": 0, "sweeper_completed": 0, "final_clean": 0,
            "marker_files_after_sweep_nonzero": 0}


def main():
    per = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    ne = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es); esx.drop_tenant(es, T)
    out = {"workload": f"{per} runs per point for injected failure and for SIGKILL of the erasing process at 4 points, plus {ne} runs with an ES error injected at a random request during the purge; PG16+key service+ES 9.5.6 on t2-dev", "results": {}}
    r = 0
    for mode in ("exception", "sigkill"):
        for point in POINTS:
            res = blank()
            for _ in range(per):
                subj, m = setup_victim(conn, lock, es, r); r += 1
                if mode == "exception":
                    try:
                        erasure.erase_subject(conn, lock, es, T, subj, fail_at=point)
                    except RuntimeError:
                        pass
                else:
                    p = subprocess.run([PY, "exp/c3_child.py", T, subj, point], capture_output=True)
                    assert p.returncode == -9, (p.returncode, p.stderr[-200:])
                finish(conn, lock, es, subj, m, res, mode, point)
            out["results"][f"{mode}:{point}"] = res; print(mode, point, res, flush=True)
    res = blank(); rng = random.Random(3)
    for _ in range(ne):
        subj, m = setup_victim(conn, lock, es, r); r += 1
        f = FlakyES(rng.randint(1, 14))
        try:
            erasure.erase_subject(conn, lock, f, T, subj)
        except (ESError, RuntimeError):
            pass
        finish(conn, lock, es, subj, m, res, "es_error", f.fail_at)
    out["results"]["es_error_injected"] = res; print("es_error", res, flush=True)
    orphans = [x["index"] for x in esx.tenant_indices(es, T) if not esx._targets(es, x["index"].replace("-events-b", "-events-w").rsplit("-g", 1)[0] + "-g*")] if False else None
    R = out["results"]
    out["pass"] = all(v["final_clean"] == v["runs"] and v["reader_leaks_while_pending"] == 0 and v["marker_files_after_sweep_nonzero"] == 0 for v in R.values())
    out["total_runs"] = sum(v["runs"] for v in R.values())
    out["necessity_of_read_filter"] = sum(v["raw_es_had_erased_docs_while_pending"] for v in R.values())
    save("C3_crash_phases", out)
    print("PASS" if out["pass"] else "FAIL", out["total_runs"])


if __name__ == "__main__":
    main()
