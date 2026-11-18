"""One node life on a fresh data volume.   python3 licence/life.py down|up N [--ttl 5|default] [--post S]
down: fresh Basic -> start_trial -> fixtures -> baseline -> restart on trial -> workload with start_basic under load -> after-ops -> restart on Basic.
up:   fresh Basic -> workload on Basic -> start_trial under load -> latch flips back.
Runs on t2-dev from ~/emlab-16096/lic/code (cwd), writes ~/emlab-16096/lic/results/<mode>_<N>.json (+ .rows.gz)."""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import exp2
import exp3
import exp4
import lib
import matrix
from lib import req

CONTAINER = "emlab-es-lic-16096"
RES = os.path.expanduser("~/emlab-16096/lic/results")
NODE_SH = os.path.expanduser("~/emlab-16096/lic/licence_node.sh")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fresh_node():
    subprocess.check_call(["bash", NODE_SH, "fresh"])
    t0 = time.time()
    ready = exp4.poll_ready(extra_s=3.0, sim=True)
    c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=90s", timeout=100)
    ready["cluster_health"] = [c, j.get("status")]
    ready["ready_after_s"] = round(time.time() - t0, 1)
    return ready


def wait_health():
    for _ in range(60):
        c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=10s", timeout=20)
        if c == 200:
            return j.get("status")
        time.sleep(1)
    return None


def stub_ok():
    try:
        with urllib.request.urlopen("http://172.17.0.1:19271/", timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


def trial_call():
    t = time.time()
    c, j, ms = req("POST", "/_license/start_trial?acknowledge=true", timeout=60)
    return {"t_req": t, "t_resp": time.time(), "status": c, "body": j}


def basic_call():
    t = time.time()
    c0, j0, _ = req("POST", "/_license/start_basic")
    t = time.time()
    c, j, ms = req("POST", "/_license/start_basic?acknowledge=true", timeout=60)
    return {"t_req": t, "t_resp": time.time(), "status": c, "body": j, "without_ack": {"status": c0, "basic_was_started": j0.get("basic_was_started"),
                                                                                       "acknowledged": j0.get("acknowledged"), "error_message": j0.get("error_message"),
                                                                                       "acknowledge_keys": sorted((j0.get("acknowledge") or {}).keys())}}


def down(n, ttl, post):
    R = {"mode": "down", "n": n, "seed": 1000 + n, "ttl": ttl, "post_s": post, "harness": {"http_patch": exp2.HTTP_PATCHED, "patch": "cached no-redirect opener replaces per-call opener (CA load 21 ms/request)"}}
    log("fresh node")
    R["stub"] = stub_ok()
    R["exp4_fresh_start"] = fresh_node()
    R["exp4_state_basic0"] = exp4.state_probe()
    matrix.make_base()
    R["exp1_basic0"] = matrix.run("basic0")
    R["stamp_basic0"] = lib.stamp()
    log("start_trial")
    R["start_trial"] = trial_call()
    R["licence_trial"] = lib.licence()
    R["exp4_state_trial"] = exp4.state_probe()
    matrix.make_base()
    R["dls_setup"] = matrix.ensure_dls()
    R["fixtures"] = exp3.fixtures()
    R["semonly_fixture"] = exp3.semonly_fixture()
    log("fixtures done; trial matrix")
    R["exp1_trial"] = matrix.run("trial")
    R["exp1_trial_quick"] = matrix.quick_probes("lic3-sem")
    R["stamp_trial"] = lib.stamp()
    # exp 3 baseline on trial: pre-reads on originals, full mutation battery on twins
    log("exp3 baseline")
    E3 = {"pre_reads": {}, "twin_mutations": {}, "twin_reads_after": {}}
    for kind in ("sem", "bbq_disk", "bbq_hnsw", "plain"):
        E3["pre_reads"][kind] = exp3.reads(kind, exp3.idx_names(kind))
        E3["twin_mutations"][kind] = exp3.mutations(kind, exp3.idx_names(kind, True))
        E3["twin_reads_after"][kind] = exp3.reads(kind, exp3.idx_names(kind, True))
    R["exp3"] = E3
    log("restart on trial")
    R["exp4_restart_trial"] = exp4.restart_and_poll(CONTAINER)
    wait_health()
    R["exp4_state_trial_after_restart"] = exp4.state_probe()
    R["semonly_trial"] = exp3.semonly_queries()
    # unrefreshed fixture is written AFTER the restart so nothing refreshes it before start_basic
    R["fixtures_unrefreshed"] = exp3.unrefreshed_fixture()
    R["unrefreshed_twin_on_trial"] = exp3.unrefreshed_partial("lic3-semu-t")
    # exp 2
    log("workload")
    W = exp2.Workload(R["seed"], ttl)
    W.setup(with_sem_index=True)
    bad = W.compute_backend_oracles()
    R["exp2_backend_oracle_failures"] = bad
    poll = exp2.Poller("lic3-sem")
    W.start()
    poll.start()
    time.sleep(25)
    R["stamp_before_transition"] = lib.stamp()
    R["dls_battery_trial"] = matrix.dls_battery()
    log("start_basic under load")
    sb = basic_call()
    R["start_basic"] = sb
    R["licence_after_start_basic"] = lib.licence()
    R["unrefreshed_after_start_basic"] = exp3.unrefreshed_partial("lic3-semu")
    time.sleep(post)
    poll.stop_ev.set()
    poll.join(10)
    W.stop()
    R["exp2"] = W.analyse(sb["t_req"], sb["t_resp"], "down")
    R["exp2"]["writes"] = W.verify_writes()
    R["exp2"]["events"] = W.events
    R["exp2"]["latch_sample_n"] = len(W.latch)
    R["exp2"]["latch_timeline"] = _latch_changes(W.latch, sb["t_resp"])
    R["exp1_moment"] = exp2.moment_analysis(poll.rows, sb["t_resp"], window=(W.events["t_start"], W.events["t_stop"]))
    R["exp1_moment_n_polls"] = len(poll.rows)
    W.dump(os.path.join(RES, "down_%d.rows.gz" % n))
    W.dump_oracles(os.path.join(RES, "down_%d.oracle.gz" % n))
    import gzip
    with gzip.open(os.path.join(RES, "down_%d.poll.gz" % n), "wt") as fh:
        json.dump(poll.rows, fh)
    log("after matrix")
    R["stamp_after"] = lib.stamp()
    R["dls_battery_after"] = matrix.dls_battery()
    R["semonly_after"] = exp3.semonly_queries()
    R["exp1_after"] = matrix.run("after")
    R["exp1_after_quick"] = matrix.quick_probes("lic3-sem")
    # exp 3 after
    log("exp3 after")
    A = {"reads": {}, "mutations": {}, "reads_after_mut": {}}
    for kind in ("sem", "bbq_disk", "bbq_hnsw", "plain"):
        ix = exp3.idx_names(kind)
        A["reads"][kind] = exp3.reads(kind, ix)
        A["mutations"][kind] = exp3.mutations(kind, ix)
        A["reads_after_mut"][kind] = exp3.reads(kind, ix)
    E3["after"] = A
    log("migrations")
    M = {}
    try:
        src = exp3.idx_names("bbq_disk")
        docs = exp3.migrate_vectors(src, "lic3-bbq_disk-mig", "hnsw")
        M["bbq_disk_to_hnsw"] = exp3.check_vectors(docs, "lic3-bbq_disk-mig")
        docs_b = exp3.migrate_vectors(src, "lic3-bbq_disk-mig-bbq", "bbq_hnsw")
        M["bbq_disk_to_bbq_hnsw"] = exp3.check_vectors(docs_b, "lic3-bbq_disk-mig-bbq")
        docs_c = exp3.migrate_vectors(src, "lic3-bbq_disk-mig-bad", "hnsw", corrupt=True)
        M["control_corrupted_migration"] = exp3.check_vectors(docs_c, "lic3-bbq_disk-mig-bad")
        # corrupted check compares dst to the CORRECT source docs, so one mismatch must be caught
        M["control_corrupted_vs_true_source"] = exp3.check_vectors(docs, "lic3-bbq_disk-mig-bad")
        pdocs = exp3.migrate_vectors(exp3.idx_names("plain"), "lic3-plain-mig", "hnsw")
        M["control_plain_to_hnsw"] = exp3.check_vectors(pdocs, "lic3-plain-mig")
        sdocs, meta = exp3.migrate_semantic(exp3.idx_names("sem"), "lic3-sem-mig")
        M["semantic_to_text_vector"] = dict(exp3.check_semantic(sdocs, "lic3-sem-mig"), **meta)
        # the migrated indices must take every Basic operation
        mm = {}
        for nm in ("lic3-bbq_disk-mig", "lic3-sem-mig"):
            r = {}
            c, j, _ = req("POST", "/%s/_update/1?refresh=true" % nm, {"doc": {"tag": "m"}}); r["update"] = c
            c, j, _ = req("DELETE", "/%s/_doc/2?refresh=true" % nm); r["delete"] = c
            c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % nm); r["force_merge"] = c
            c, j, _ = req("PUT", "/%s/_doc/new1?refresh=true" % nm, {"title": "x", "tag": "n"}); r["index_new"] = c
            mm[nm] = r
        M["migrated_basic_ops"] = mm
    except Exception as e:  # noqa: BLE001
        M["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        M["trace"] = traceback.format_exc()[-800:]
    E3["migration"] = M
    R["exp3"] = E3
    # exp 4 on Basic
    log("exp4 basic")
    R["exp4_state_basic_after"] = exp4.state_probe()
    R["exp4_units"] = exp4.unit_cases()
    counts_before = {k: exp3.count(exp3.idx_names(k)) for k in ("sem", "bbq_disk", "bbq_hnsw", "plain")}
    R["exp4_restart_basic"] = exp4.restart_and_poll(CONTAINER)
    wait_health()
    E3["after_restart"] = {"counts_before": counts_before, "counts_after": {k: exp3.count(exp3.idx_names(k)) for k in counts_before},
                           "reads": {k: exp3.reads(k, exp3.idx_names(k)) for k in ("sem", "bbq_disk")}}
    c, j, _ = req("GET", "/_cat/indices/lic3-*?format=json&h=index,health,docs.count,store.size")
    E3["after_restart"]["cat"] = j if c == 200 else None
    R["second_start_trial"] = trial_call()
    R["trial_status"] = req("GET", "/_license/trial_status")[1]
    R["licence_end"] = lib.licence()
    return R


def _latch_changes(lat, t_ref):
    out, last = [], object()
    for t, p, loc, lp in lat:
        k = (p, loc)
        if k != last:
            out.append([round(t - t_ref, 2), p, loc])
            last = k
    return out[:30]


def up(n, ttl, post):
    R = {"mode": "up", "n": n, "seed": 2000 + n, "ttl": ttl, "post_s": post, "harness": {"http_patch": exp2.HTTP_PATCHED, "patch": "cached no-redirect opener replaces per-call opener (CA load 21 ms/request)"}}
    log("fresh node")
    R["stub"] = stub_ok()
    R["exp4_fresh_start"] = fresh_node()
    R["exp4_state_basic0"] = exp4.state_probe()
    matrix.make_base()
    R["exp1_basic0"] = matrix.run("basicb")
    W = exp2.Workload(R["seed"], ttl)
    W.setup(with_sem_index=True)
    poll = exp2.Poller("lic-sem2")
    W.start()
    poll.start()
    time.sleep(25)
    R["stamp_before_transition"] = lib.stamp()
    log("start_trial under load")
    st = trial_call()
    R["start_trial"] = st
    R["licence_after_start_trial"] = lib.licence()
    t = time.time()
    c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_body())
    R["endpoint_create_after_trial"] = {"t_rel_resp_s": round(t - st["t_resp"], 2), "status": c, "error": lib.err(j)}
    time.sleep(post)
    poll.stop_ev.set()
    poll.join(10)
    W.stop()
    bad = W.compute_backend_oracles()
    R["exp2_backend_oracle_failures"] = bad
    R["exp2"] = W.analyse(st["t_req"], st["t_resp"], "up")
    R["exp2"]["writes"] = W.verify_writes()
    R["exp2"]["events"] = W.events
    R["exp2"]["latch_timeline"] = _latch_changes(W.latch, st["t_resp"])
    sw = R["exp2"]["writes"].get("sem_first_ok_t")
    R["exp2"]["sem_first_ok_after_trial_s"] = None if sw is None else round(sw - st["t_resp"], 2)
    R["exp1_moment"] = exp2.moment_analysis(poll.rows, st["t_resp"], features=("rrf", "linear", "semantic_query", "inference_use"), direction="up",
                                           window=(W.events["t_start"], W.events["t_stop"]))
    W.dump(os.path.join(RES, "up_%d.rows.gz" % n))
    W.dump_oracles(os.path.join(RES, "up_%d.oracle.gz" % n))
    import gzip
    with gzip.open(os.path.join(RES, "up_%d.poll.gz" % n), "wt") as fh:
        json.dump(poll.rows, fh)
    R["stamp_after"] = lib.stamp()
    R["exp1_trial_after_up"] = matrix.run("trialb")
    R["exp4_state_trial"] = exp4.state_probe()
    R["second_start_trial"] = trial_call()
    R["licence_end"] = lib.licence()
    return R


def main():
    mode, n = sys.argv[1], int(sys.argv[2])
    ttl = 5.0
    post = 70
    for i, a in enumerate(sys.argv):
        if a == "--ttl":
            ttl = 300.0 if sys.argv[i + 1] == "default" else float(sys.argv[i + 1])
        if a == "--post":
            post = int(sys.argv[i + 1])
    os.makedirs(RES, exist_ok=True)
    out = os.path.join(RES, "%s_%d.json" % (mode, n))
    try:
        R = (down if mode == "down" else up)(n, ttl, post)
        R["status"] = "complete"
    except Exception as e:  # noqa: BLE001
        R = {"status": "crashed", "error": "%s: %s" % (type(e).__name__, str(e)[:500]), "trace": traceback.format_exc()[-2000:]}
    R["stamp_end"] = lib.stamp({"mode": mode, "n": n, "ttl": ttl, "post": post})
    sha = lib.save(out, R)
    log("saved", out, sha[:12], R["status"])


if __name__ == "__main__":
    main()
