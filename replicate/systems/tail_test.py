"""DESIGN 3.2.4 and 3.1.10 (THRESHOLDS.md section V). Owner memsys at a13b524 (store, projector, reader) unmodified,
own Postgres and own Elasticsearch. Projector runs in a child process driven by this script (owner's
exp/projector_main.py is not used). Timing figures are INDICATIVE (shared host).

usage (systems venv): python tail_test.py
"""
import datetime as dt
import multiprocessing as mp
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysdrv  # noqa: E402
from memsys import db, esx, projector, reader, store  # noqa: E402

T = "reptail"


def mkev(text):
    return dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message",
                subject_id="u1", content=text)


def tok():
    return "zq" + uuid.uuid4().hex[:10]


def proj_loop(stop):
    conn, lock, es = sysdrv.conns()
    p = projector.Projector(conn, lock, es, tenants=[T], batch=500)
    while not stop.is_set():
        if p.run_once() == 0:
            time.sleep(0.05)


class Proj:
    def __init__(self):
        self.stop, self.p = None, None

    def start(self):
        self.stop = mp.Event()
        self.p = mp.Process(target=proj_loop, args=(self.stop,))
        self.p.start()

    def halt(self):
        self.stop.set()
        self.p.join(30)


def es_count(es, t):
    st, j = es.req("POST", "/sys-%s-events/_search" % T, {"size": 0, "track_total_hits": True, "query": {"match": {"content": t}}}, ok=None)
    return j.get("hits", {}).get("total", {}).get("value", 0) if st == 200 else None


def found(r, eid):
    h = next((h for h in r["hits"] if h["id"] == eid), None)
    return (h is not None), (h["via"] if h else None)


def main():
    conn, lock, es = sysdrv.conns()
    db.reset_schema(conn)
    esx.install_templates(es)
    esx.drop_tenant(es, T)
    w = db.connect(autocommit=True)
    store.append_events(w, T, "warm", [mkev("warm up event")])
    pr = Proj()
    pr.start()
    t0 = time.time()
    while es_count(es, "warm") in (None, 0) and time.time() - t0 < 60:
        time.sleep(0.2)
    rd = reader.Reader(conn, es)
    out = {}
    # A. read-after-write, projector running
    A = {"trials": 100, "with_tail": 0, "via": {"es": 0, "tail": 0}, "control_tail_off_found": 0, "items": []}
    for i in range(100):
        t = tok()
        ids, _ = store.append_events(w, T, "trial", [mkev("trial %s marker" % t)])
        f1, via = found(rd.search(T, t, k=10), ids[0])
        f2, _ = found(rd.search(T, t, k=10, tail=False), ids[0])
        A["with_tail"] += f1
        if via:
            A["via"][via] += 1
        A["control_tail_off_found"] += f2
        A["items"].append({"event_id": ids[0], "found_with_tail": f1, "via": via, "found_tail_off": f2})
        time.sleep(0.05)
    out["A_read_after_write"] = A
    print({k: v for k, v in A.items() if k != "items"}, flush=True)
    # B. projector paused, 600 events written
    pr.halt()
    hwm_before = rd.hwm(T)
    toks, ids = [], []
    for i in range(600):
        t = tok()
        ii, _ = store.append_events(w, T, "paused%d" % (i % 5), [mkev("paused %s note" % t)])
        toks.append(t)
        ids.append(ii[0])
    hwm_after = rd.hwm(T)
    rd300 = reader.Reader(conn, es, tail_bound=300)
    fb = f300 = trunc300 = es_found = 0
    tb = time.time()
    for t, eid in zip(toks, ids):
        r = rd.search(T, t, k=10)
        fb += found(r, eid)[0]
        r3 = rd300.search(T, t, k=10)
        f300 += found(r3, eid)[0]
        trunc300 += bool(r3["tail"]["truncated"])
        es_found += found(rd.search(T, t, k=10, tail=False), eid)[0]
    out["B_paused"] = {"events_written_while_paused": 600, "hwm_unchanged": hwm_before == hwm_after,
                       "found_with_default_tail_bound_2000": fb, "control_tail_off_found": es_found,
                       "control_tail_bound_300": {"found": f300, "flagged_truncated": trunc300, "of": 600},
                       "search_loop_s": round(time.time() - tb, 1)}
    print(out["B_paused"], flush=True)
    pr.start()
    t1 = time.time()
    while es_count(es, toks[-1]) in (None, 0) and time.time() - t1 < 120:
        time.sleep(0.1)
    out["B_paused"]["catchup_s_indicative"] = round(time.time() - t1, 2)

    # C/D. long transaction in the same database, then in a second database of the same cluster
    def hold(dbname, label, hold_s=12):
        hc = db.connect(autocommit=False)
        if dbname != "emlab":
            hc.close()
            import psycopg
            port = int(open(os.path.join(os.environ["MEMSYS_HOME"], "pg_port")).read())
            hc = psycopg.connect(host="127.0.0.1", port=port, dbname=dbname, user="emlab", autocommit=False)
            hc.execute("create table if not exists holdx(i int)")
            hc.execute("insert into holdx values (1)")
        else:
            hc.execute("insert into sessions(tenant_id, session_id) values ('holdtenant', %s)", ("h" + uuid.uuid4().hex[:6],))
        xid = hc.execute("select pg_current_xact_id()::text").fetchone()[0]
        time.sleep(0.5)
        t = tok()
        ids, _ = store.append_events(w, T, "probe", [mkev("probe %s during hold" % t)])
        th = time.time()
        samples = []
        while time.time() - th < hold_s:
            samples.append((round(time.time() - th, 1), es_count(es, t)))
            time.sleep(1)
        es_during = es_count(es, t)
        f_tail, via = found(rd.search(T, t, k=10), ids[0])
        f_off, _ = found(rd.search(T, t, k=10, tail=False), ids[0])
        hc.commit()
        hc.close()
        tc = time.time()
        while es_count(es, t) in (None, 0) and time.time() - tc < 60:
            time.sleep(0.05)
        catch = round(time.time() - tc, 2)
        return {"database_holding": dbname, "label": label, "holder_xid": xid, "hold_s": hold_s,
                "es_docs_for_probe_at_end_of_hold": es_during, "es_samples_during_hold": samples,
                "reader_with_tail_found_probe": f_tail, "via": via, "control_tail_off_found_probe": f_off,
                "es_docs_after_commit": es_count(es, t), "catchup_s_indicative": catch}
    out["C_hold_same_database"] = hold("emlab", "same database")
    print(out["C_hold_same_database"], flush=True)
    out["D_hold_second_database"] = hold("other", "second database, same cluster")
    print(out["D_hold_second_database"], flush=True)
    pr.halt()
    A_, B_, C_, D_ = out["A_read_after_write"], out["B_paused"], out["C_hold_same_database"], out["D_hold_second_database"]
    checks = {
        "3.2.4a_with_tail_100_of_100": A_["with_tail"] == 100,
        "3.2.4a_control_tail_off_le_10": A_["control_tail_off_found"] <= 10,
        "3.2.4b_paused_600_of_600": B_["found_with_default_tail_bound_2000"] == 600 and B_["hwm_unchanged"],
        "3.2.4b_control_bound300_le_300_and_flagged": B_["control_tail_bound_300"]["found"] <= 300 and B_["control_tail_bound_300"]["flagged_truncated"] > 0,
        "3.1.10_tail_finds_probe_during_hold": C_["reader_with_tail_found_probe"],
        "3.1.10_control_search_only_misses_during_hold": not C_["control_tail_off_found_probe"],
        "3.1.10_projection_stalled_during_hold": C_["es_docs_for_probe_at_end_of_hold"] == 0,
        "extension_second_database_hold_stalls_projection": D_["es_docs_for_probe_at_end_of_hold"] == 0}
    out = {"stamp": sysdrv.stamp(__file__, {"tenant": T, "tail_bound_default": 2000, "batch": 500}, conn, es), **out,
           "threshold_checks": checks}
    p = sysdrv.save("tail_3_2_4_3_1_10.json", out)
    print(checks, "saved", p)


if __name__ == "__main__":
    main()
