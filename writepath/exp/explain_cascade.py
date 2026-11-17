"""Diagnosis (coordinator item 2): which statement inside one cascade batch grows with the TENANT size?
Builds a tenant of N documents (filler over 1,000 other subjects, as exp/erasure_cost.py) with subject S of K events,
marks S, then runs cascade batches as the admin with auto_explain (nested statements, ANALYZE) and prints, per batch,
the wall time and the slowest nested statements with their plans' top lines, read back from the server log.
Batch sizes per batch index: batch, batch, batch/5, batch/5, then ANALYZE, then the same four (does the cost
scale with the rows of the batch, and does fresh statistics change it?). Exploratory: no PASS. usage: python -m exp.explain_cascade N K [batch]"""
import subprocess
import sys
import time

from exp.erasure_cost import _build
from exp.util import tenant, env
from wp import db, erasure

from wp import config  # noqa: E402

PG_C = config.PG_CONTAINER


def main(n, k, batch):
    conn, ks, es = env()
    t = tenant("xc")
    print("build", _build(t, n, {"S": k}), flush=True)
    st = db.admin().execute("select relname, n_live_tup, n_dead_tup, last_analyze, last_autoanalyze, last_autovacuum from pg_stat_user_tables "
                            "where relname in ('events','facts','analyst_jobs','tombstones') order by 1").fetchall()
    print("stats_before", st, flush=True)
    jid = erasure.request(conn, t, "S")
    adm = db.admin()
    adm.execute("select wp_erasure_mark(%s)", (jid,))
    adm.execute("select wp_erasure_rescan_done(%s, coalesce((select ident_xmax from erasure_jobs where job_id=%s), 0), "
                "(select e.matcher from identifier_epochs e join erasure_jobs j on j.tenant_id = e.tenant_id where j.job_id=%s))", (jid, jid, jid))
    adm.execute("load 'auto_explain'")
    for k_, v in (("auto_explain.log_min_duration", "200ms"), ("auto_explain.log_analyze", "on"), ("auto_explain.log_nested_statements", "on"),
                  ("auto_explain.log_buffers", "on"), ("statement_timeout", "0")):
        adm.execute(f"set {k_} = '{v}'")
    i, n_ = 0, 1
    while n_ and i < 8:
        if i == 4:                                       # does a manual ANALYZE change the plan or the time?
            t0 = time.time()
            for tb in ("events", "facts", "analyst_jobs", "tombstones", "erasure_mentions", "subjects"):
                adm.execute(f"analyze {tb}")
            print("ANALYZE done", round(time.time() - t0, 2), "s", flush=True)
        time.sleep(1.1)                                  # earlier batches' log lines fall before `since`
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        t0 = time.time()
        n_ = adm.execute("select wp_erasure_cascade(%s, %s)", (jid, batch if i % 4 < 2 else batch // 5)).fetchone()[0]
        dt = round(time.time() - t0, 3)
        time.sleep(0.5)
        log = subprocess.run(["docker", "logs", "--since", since, PG_C], capture_output=True, text=True).stderr
        stmts = []
        for block in log.split("LOG:  duration: ")[1:]:
            head = block.split("\n")
            dur = float(head[0].split(" ms")[0])
            q = next((ln.strip() for ln in head if ln.strip().startswith("Query Text:")), "")[:200]
            plan = [ln.rstrip()[:180] for ln in head[1:40] if ln.strip() and not ln.startswith("20")][:20]
            stmts.append((dur, q, plan))
        stmts.sort(reverse=True)
        print(f"batch {i} rows={n_} wall={dt}s ({round(dt / max(n_, 1) * 1000, 3)} ms/row)", flush=True)
        for dur, q, plan in stmts[:3]:
            print(f"   {dur:.0f} ms  {q}", flush=True)
            for ln in plan:
                print("        ", ln, flush=True)
        i += 1


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 5000)
