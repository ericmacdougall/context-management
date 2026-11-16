"""Top-30 row 23 (Postgres part): a transaction that commits AFTER later ones must not be skipped by the projector.
H appends one event and stalls before commit (named hook); meanwhile 10 appends on another session commit. Projection
passes run during the stall and after H commits.
  control (naive): cursor = max ingest_seq seen, reads `ingest_seq > cursor` -> must skip H's event.
  fenced (wp.projector): reads only below pg_snapshot_xmin -> must project all 11, H's first in commit order.
The PgBouncer half of row 23 is NOT run (no pooler in this environment). usage: python -m exp.row23_latecommit [R]"""
import sys
import threading

from exp.common import write_result
from exp.util import tenant, msg, session, es_count_live
from wp import db, keys as K, log, projector
from wp.es import ES


def one():
    conn, ks, es = db.connect(), K.KeyService(), ES()
    t = tenant("r23")
    sH, s2 = session(conn, t, "hold"), session(conn, t, "s2")
    held, release, out = threading.Event(), threading.Event(), {}

    def hook(name, data=None):
        if name == "before_commit":
            held.set(); release.wait(30)

    hc = db.connect()
    # The app role's idle_in_transaction_session_timeout (5 s, review F6) would end H's held transaction before the 10
    # later commits and projection passes finish; this experiment needs H to stay open, so H's session raises it.
    hc.execute("set idle_in_transaction_session_timeout = '120s'")
    th = threading.Thread(target=lambda: out.setdefault("H", log.append(hc, K.KeyService(), sH, [msg(0, "late committer zqlate", "UH")], hook=hook)))
    th.start(); held.wait(30)
    for i in range(10):
        log.append(conn, ks, s2, [msg(1 + i, f"early committer {i}", "U2")])
    naive_cursor, naive_seen = 0, set()

    def naive_pass():
        nonlocal naive_cursor
        rows = conn.execute("select event_id, ingest_seq from events where tenant_id=%s and ingest_seq > %s order by ingest_seq", (t, naive_cursor)).fetchall()
        for eid, s in rows:
            naive_seen.add(eid); naive_cursor = max(naive_cursor, s)
    p = projector.Projector(db.connect(), K.KeyService(), es, t)
    naive_pass(); p.run()
    during = {"naive_seen": len(naive_seen), "xmin_projected": es_count_live(es, t, "events", {"match_all": {}})}
    release.set(); th.join(30)
    naive_pass(); p.run(); p.stop()
    h_id = out["H"]["accepted"][0]
    total = conn.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0]
    xmin_n = es_count_live(es, t, "events", {"match_all": {}})
    h_in_es = es_count_live(es, t, "events", {"term": {"event_id": h_id}})
    first = conn.execute("select event_id from events where tenant_id=%s order by commit_xid, ingest_seq limit 1", (t,)).fetchone()[0]
    return {"events_total": total, "during_stall": during, "naive_projected": len(naive_seen), "naive_skipped_H": h_id not in naive_seen,
            "xmin_projected": xmin_n, "xmin_has_H": h_in_es == 1, "H_first_in_commit_xid_order": first == h_id}


if __name__ == "__main__":
    R = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    runs = [one() for _ in range(R)]
    res = {"R": R, "control_violated": sum(r["naive_skipped_H"] for r in runs), "fenced_holds": sum(r["xmin_has_H"] and r["xmin_projected"] == 11 for r in runs),
           "runs": runs}
    res["pass"] = res["fenced_holds"] == R and res["control_violated"] >= int(0.9 * R)
    print({k: v for k, v in res.items() if k != "runs"}, runs[0])
    write_result("row23_latecommit", res)
