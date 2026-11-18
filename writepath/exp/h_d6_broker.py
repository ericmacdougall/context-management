"""Amendment H6 (D6): the key broker, proved with distinct uids in this stream's own containers (the H2 pattern).

  emlab2-wphard-broker   uid 20004  WP_SECRETS_DIR=/svc/broker (es_broker_password, pgpass.wp_broker)
                                    python -m wp.broker serve /run/wphard/b/broker.sock   (allows uid 20001 only)
  emlab2-wphard-keysvc   uid 20003  WP_SECRETS_DIR=/svc/keysvc (pgpass.wp_kms)   python -m wp.keysvc serve (C7 decodes)
  emlab2-wphard-app      uid 20001  WP_SECRETS_DIR=/svc/app (pgpass.wp_app; NO Elasticsearch credential in the fenced
                                    arm; the projector role key in the control arm)
                                    one-shot per run: python -m exp.h_d6_broker child <arm> <json args>
The operator (this process: setup, projection of tenants A and B, witnesses, descriptor reads, broker stop and start)
uses the lab's admin credentials, which the app container gets read-only at /operator/secrets for C9 only.
Cases C1 to C13 and the verdict rule are in exp/THRESHOLDS.md amendment H6. Every encoded key stays in the child's
memory; the child prints only key ids and, on a separate line the orchestrator keeps in memory and never writes, the
sha256 of each key value, so C13 can look for the values in the broker log, the child output and the result.
usage: python -m exp.h_d6_broker [R]"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

UIDS = {"app": 20001, "verifier": 20002, "keysvc": 20003, "broker": 20004}
KS_SOCK = "/run/wphard/keysvc.sock"
B_SOCK = "/run/wphard/b/broker.sock"
C12_DIR = "/run/wphard/c12"
N_STALE = 20
N_PAIRS = 20
PROBE_FILES = {"broker_es_pw_file": "/svc/broker/es_broker_password", "broker_pgpass": "/svc/broker/pgpass.wp_broker",
               "elastic_pw_file": "/operator/secrets/es_elastic_password",
               "admin_es_key": "/operator/secrets/es_api_key.json", "admin_pgpass": "/operator/secrets/pgpass",
               "operator_broker_pw_file": "/operator/secrets/es_broker_password"}
TOKEN = re.compile(rb"[A-Za-z0-9+/=_-]{20,}")


def _sha(v):
    return hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()


# ------------------------------------------------------------------ inside the application container
def _w(es, target, doc_id, alias=True):
    """One document write; (status, error type). alias targets carry require_alias (no index is ever created)."""
    from wp.es import _etype
    kw = {"require_alias": "true"} if alias else {}
    s, j = es.req("PUT", f"/{target}/_create/{doc_id}", {"event_id": doc_id, "kind": "h6probe"}, ok=None, **kw)
    return [s, None if s in (200, 201) else _etype(j)]


def _alias_req(es, actions):
    from wp.es import _etype
    s, j = es.req("POST", "/_aliases", {"actions": actions}, ok=None)
    return [s, None if s == 200 and not j.get("errors") else _etype(j) or "errors"]


def _files():
    import errno
    out = {}
    for name, p in PROBE_FILES.items():
        try:
            with open(p, "rb") as f:
                f.read(1)
            out[name] = "readable"
        except OSError as e:
            out[name] = errno.errorcode.get(e.errno, str(e.errno))
    return out


def _err(e):
    return f"{type(e).__name__}: {str(e)[:80]}"


def child_fenced(a):
    from wp import broker as B, db, families as F, keysvc, lease as L, projector
    A, Bt, C, eb = a["A"], a["B"], a["C"], a["eb"]
    seen, res = [], {"uid": os.getuid(), "files": _files(), "invalid": {}}
    conn = db.connect(application_name="h6-app")
    bc = B.Client(B_SOCK)

    def key(t, lease, case):
        try:
            k = bc.writer_key(t, lease.epoch, lease.holder)
        except Exception as e:  # noqa: BLE001
            res["invalid"][case] = _err(e)
            return None
        seen.append(k["encoded"])
        return k

    la = L.acquire(conn, A, L.PROJECTOR, 120.0)
    if la is None:
        res["invalid"]["lease_A"] = "not acquired"
    k1 = key(A, la, "mint_A") if la else None
    if k1:
        E = la.epoch
        es1 = B.es_with_key(k1, "projector")
        res["key_A_id"], res["E"] = k1["id"], E
        res["own_write"] = _w(es1, F.write_alias(A, "events", E), "h6-own")
        res["C1"] = {"B_w": _w(es1, F.write_alias(Bt, "events", eb), "h6-c1w"),
                     "B_read": _w(es1, F.read_alias(Bt, "events"), "h6-c1r"),
                     "B_x": _w(es1, F.erase_alias(Bt, "events"), "h6-c1x")}
        res["C2"] = {"A_base": _w(es1, F.base_index(A, "events"), "h6-c2", alias=False)}
        res["C3"] = {"A_x": _w(es1, F.erase_alias(A, "events"), "h6-c3")}
        idx = F.base_index(A, "events")
        res["C4"] = {"add_next": _alias_req(es1, [{"add": {"index": idx, "alias": F.write_alias(A, "events", E + 1)}}]),
                     "remove_own": _alias_req(es1, [{"remove": {"index": idx, "alias": F.write_alias(A, "events", E)}}]),
                     "add_on_B": _alias_req(es1, [{"add": {"index": F.base_index(Bt, "events"), "alias": F.write_alias(Bt, "events", eb + 1)}}])}
        # C5: h2 takes the lease by force; the broker's CAS moves -w; h1's key then writes through -w<E>
        l2 = L.acquire(conn, A, L.PROJECTOR, 120.0, force=True)
        k2 = key(A, l2, "mint_A_h2") if l2 else None
        if k2:
            res["C5"] = {"E2": l2.epoch, "h2_write": _w(B.es_with_key(k2, "projector"), F.write_alias(A, "events", l2.epoch), "h6-c5own"),
                         "stale": [_w(es1, F.write_alias(A, "events", E), f"h6-c5-{i}") for i in range(N_STALE)]}
            res["C5"]["stale_accepted"] = sum(1 for s, _ in res["C5"]["stale"] if s in (200, 201))
            res["C6"] = {}
            for name, (ep, who) in {"old_epoch_h1": (E, la.holder), "new_epoch_h1": (l2.epoch, la.holder),
                                    "new_epoch_empty": (l2.epoch, "")}.items():
                try:
                    bc.writer_key(A, ep, who)
                    res["C6"][name] = "issued"
                except B.Refused as e:
                    res["C6"][name] = f"Refused: {e}"
                except Exception as e:  # noqa: BLE001
                    res["C6"][name] = _err(e)
        elif l2 is None:
            res["invalid"]["lease_A_h2"] = "not acquired"
    # C8: concurrent issuance for A and B on two connections
    lb = L.acquire(conn, Bt, L.PROJECTOR, 120.0)
    la8 = L.acquire(conn, A, L.PROJECTOR, 120.0, force=True)
    if lb is None or la8 is None:
        res["invalid"]["lease_C8"] = "not acquired"
    else:
        got, errs = [], []
        gate = threading.Barrier(2)

        def worker(t, lease):
            c = B.Client(B_SOCK)
            for _ in range(N_PAIRS):
                gate.wait(30)
                try:
                    k = c.writer_key(t, lease.epoch, lease.holder)
                    seen.append(k["encoded"])
                    got.append([t, lease.epoch, k["id"]])
                except Exception as e:  # noqa: BLE001
                    errs.append(_err(e))
            c.close()
        ts = [threading.Thread(target=worker, args=(A, la8)), threading.Thread(target=worker, args=(Bt, lb))]
        [t.start() for t in ts]
        [t.join(300) for t in ts]
        res["C8"] = {"keys": got, "errors": errs[:5], "n_errors": len(errs)}
    # C7: rotation with the real projector in broker mode (tenant C: 5 events, not projected yet)
    ks = keysvc.Client(KS_SOCK)
    try:
        p1 = projector.Projector(db.connect(application_name="h6-p1"), ks, None, C, batch=2, broker=B.Client(B_SOCK))
        if not p1.start(force=True):
            raise RuntimeError("P1 did not acquire")
        seen.append(p1.es.auth.split(" ", 1)[1])
        n1 = p1.step()
        w_before = p1.stats["docs_written"]
        p2 = projector.Projector(db.connect(application_name="h6-p2"), ks, None, C, batch=2, broker=B.Client(B_SOCK))
        if not p2.start(force=True):
            raise RuntimeError("P2 did not acquire")
        seen.append(p2.es.auth.split(" ", 1)[1])
        r1 = p1.step()
        done = p2.run(max_rounds=500)
        res["C7"] = {"p1_first_step": n1, "p1_step_after_takeover": r1, "p1_written_before": w_before,
                     "p1_written_after": p1.stats["docs_written"], "p2_done": done, "p2_written": p2.stats["docs_written"],
                     "p1_key_id": p1.key_id, "p2_key_id": p2.key_id}
        p2.stop()
    except Exception as e:  # noqa: BLE001
        res["invalid"]["C7_setup"] = _err(e)
    _emit("H6CHILD", res, seen)


def child_control(a):
    from wp import db, families as F, lease as L
    from wp.es import ES, ESError
    A, Bt, eb = a["A"], a["B"], a["eb"]
    res = {"uid": os.getuid(), "invalid": {}}
    es = ES(role="projector")                 # the pre-broker deployment: the app holds the projector role key
    conn = db.connect(application_name="h6-ctl")
    la = L.acquire(conn, A, L.PROJECTOR, 120.0)
    # H6a: with security on, ES refuses this key's alias CAS ("an alias that has more permissions than any of the
    # indices": create_doc on -w, none on the base index). Recorded as a finding of the pre-broker design; the cases
    # still run, since the key writes through any tenant's existing -w alias without a CAS of its own.
    res["findings"] = {}
    try:
        F.fence_es(es, A, la.epoch)
        res["findings"]["fence_A"] = "ok"
    except (ESError, F.StaleEpoch, F.ProjectionError) as e:
        res["findings"]["fence_A"] = _err(e)
    E = la.epoch
    idx = F.base_index(A, "events")
    res["own_write"] = _w(es, F.write_alias(A, "events", E), "h6-own")
    res["C1"] = {"B_w": _w(es, F.write_alias(Bt, "events", eb), "h6-c1w"),
                 "B_read": _w(es, F.read_alias(Bt, "events"), "h6-c1r"),
                 "B_x": _w(es, F.erase_alias(Bt, "events"), "h6-c1x")}
    res["C2"] = {"A_base": _w(es, idx, "h6-c2", alias=False)}
    res["C3"] = {"A_x": _w(es, F.erase_alias(A, "events"), "h6-c3")}
    res["C4"] = {"add_on_B": _alias_req(es, [{"add": {"index": F.base_index(Bt, "events"), "alias": F.write_alias(Bt, "events", eb + 1)}}]),
                 "add_next": _alias_req(es, [{"add": {"index": idx, "alias": F.write_alias(A, "events", E + 1)}}])}
    l2 = L.acquire(conn, A, L.PROJECTOR, 120.0, force=True)
    try:
        F.fence_es(es, A, l2.epoch)
        res["findings"]["fence_A_h2"] = "ok"
    except (ESError, F.StaleEpoch, F.ProjectionError) as e:
        res["findings"]["fence_A_h2"] = _err(e)
    res["C5"] = {"E2": l2.epoch, "readd_old": _alias_req(es, [{"add": {"index": idx, "alias": F.write_alias(A, "events", E)}}]),
                 "stale": [_w(es, F.write_alias(A, "events", E), f"h6-c5-{i}") for i in range(N_STALE)]}
    res["C5"]["stale_accepted"] = sum(1 for s, _ in res["C5"]["stale"] if s in (200, 201))
    _emit("H6CHILD", res, [])


def child_c12(a):
    """C12 in its own container: phases synchronised with the orchestrator through files in C12_DIR."""
    from wp import broker as B, db, families as F, lease as L, projector
    D = a["D"]
    seen, res = [], {"invalid": {}}
    conn = db.connect(application_name="h6-c12")

    def wait_for(name, limit=300):
        t0 = time.time()
        while not os.path.exists(os.path.join(C12_DIR, name)):
            if time.time() - t0 > limit:
                raise TimeoutError(name)
            time.sleep(0.5)

    def mark(name):
        pathlib.Path(C12_DIR, name).write_text("1")

    l1 = L.acquire(conn, D, L.PROJECTOR, 600.0)
    try:
        k1 = B.Client(B_SOCK).writer_key(D, l1.epoch, l1.holder)
    except Exception as e:  # noqa: BLE001
        res["invalid"]["mint_D"] = _err(e)
        mark("phase1"); mark("phase2")
        _emit("H6C12", res, seen)
        return
    seen.append(k1["encoded"])
    es1 = B.es_with_key(k1, "projector")
    E = l1.epoch
    res["before_stop"] = _w(es1, F.write_alias(D, "events", E), "h6-12-0")
    mark("phase1")
    wait_for("stopped")
    res["a_window_write"] = _w(es1, F.write_alias(D, "events", E), "h6-12-a")            # EXPECTED accepted (H6)
    l2 = L.acquire(conn, D, L.PROJECTOR, 600.0, force=True)
    try:
        p = projector.Projector(db.connect(application_name="h6-c12p"), None, None, D, broker=B.Client(B_SOCK))
        res["b_projector_start"] = "started" if p.start() else "not acquired"
    except Exception as e:  # noqa: BLE001
        res["b_projector_start"] = type(e).__name__
    mark("phase2")
    wait_for("restarted")
    try:
        k2 = B.Client(B_SOCK).writer_key(D, l2.epoch, l2.holder)
        seen.append(k2["encoded"])
        res["c_h2_key_id"] = k2["id"]
        res["c_stale"] = [_w(es1, F.write_alias(D, "events", E), f"h6-12-c{i}") for i in range(N_STALE)]
        res["c_stale_accepted"] = sum(1 for s, _ in res["c_stale"] if s in (200, 201))
    except Exception as e:  # noqa: BLE001
        res["invalid"]["mint_D_h2"] = _err(e)
    _emit("H6C12", res, seen)


def peer(t):
    """Inside a container of uid 20002 (not on the broker's allow list)."""
    from wp import broker as B
    try:
        B.Client(B_SOCK).writer_key(t, 1, "h6-peer")
        print("H6PEER issued", flush=True)
    except Exception as e:  # noqa: BLE001
        print("H6PEER " + _err(e), flush=True)


def _emit(tag, res, seen):
    """Print the result (key values must not be in it) and the values' hashes on a separate line."""
    out = json.dumps(res, sort_keys=True)
    res_leak = sum(1 for v in seen if v and v in out)
    if res_leak:
        out = json.dumps({"emit_refused": f"{res_leak} key values in the result"})
    print(f"{tag} {out}", flush=True)
    print(f"{tag}HASH " + json.dumps(sorted({_sha(v) for v in seen if v})), flush=True)


# ------------------------------------------------------------------ orchestrator (operator)
def _h2():
    import exp.h_d2_uids as H2
    return H2


def prepare(arm):
    from wp import config
    H2 = _h2()
    have = set(os.listdir(config.SECRETS))
    sets = {"app": (UIDS["app"], ["pgpass.wp_app"] + (["es_api_key.projector.json"] if arm == "control" else [])),
            "keysvc": (UIDS["keysvc"], ["pgpass.wp_kms"]),
            "broker": (UIDS["broker"], ["es_broker_password", "pgpass.wp_broker"])}
    lines = ["set -e", "rm -rf /h/svc /h/run", "install -d -m 755 /h/svc",
             f"install -d -o {UIDS['keysvc']} -g {UIDS['keysvc']} -m 755 /h/run",
             f"install -d -o {UIDS['broker']} -g {UIDS['broker']} -m 755 /h/run/b", "install -d -m 0777 /h/run/c12"]   # not sticky: the operator clears the child's phase files
    for d, (uid, files) in sets.items():
        lines.append(f"install -d -o {uid} -g {uid} -m 700 /h/svc/{d}")
        lines += [f"install -o {uid} -g {uid} -m 600 /h/secrets/{f} /h/svc/{d}/{f}" for f in files if f in have]
    H2._root("\n".join(lines))
    return {d: [f for f in files if f in have] for d, (_, files) in sets.items()}


def witness():
    paths = {n: p.replace("/svc/", "/h/svc/").replace("/operator/secrets/", "/h/secrets/") for n, p in PROBE_FILES.items()}
    out = _h2()._root("; ".join(f"[ -s {p} ] && echo {n}=yes || echo {n}=no" for n, p in paths.items()))
    return dict(ln.split("=") for ln in out.split())


def _wait_log(name, text, count=1, limit=90):
    H2 = _h2()
    t0 = time.time()
    while H2._docker("logs", name, check=False).stdout.count(text) < count:
        if time.time() - t0 > limit:
            raise RuntimeError(f"{name} did not log {text!r}: " + H2._docker("logs", "--tail", "3", name, check=False).stderr[-300:])
        time.sleep(1)


def start_services():
    H2 = _h2()
    stop_services()
    H2._docker(*H2._run_args("emlab2-wphard-keysvc", UIDS["keysvc"], "keysvc", ["-d", "-e", f"WP_KEYSVC_ALLOW_UIDS={UIDS['app']}"]),
               "-m", "wp.keysvc", "serve", KS_SOCK)
    H2._docker(*H2._run_args("emlab2-wphard-broker", UIDS["broker"], "broker", ["-d", "-e", f"WP_BROKER_ALLOW_UIDS={UIDS['app']}"]),
               "-m", "wp.broker", "serve", B_SOCK)
    _wait_log("emlab2-wphard-keysvc", "keysvc serving")
    _wait_log("emlab2-wphard-broker", "broker serving")


def stop_services():
    for n in ("keysvc", "broker", "app", "app12", "peer"):
        _h2()._docker("rm", "-f", f"emlab2-wphard-{n}", check=False)


def _project(conn, ks, es, t, n=5, project=True):
    from exp.util import msg, session
    from wp import log, projector
    s = session(conn, t)
    log.append(conn, ks, s, [msg(j, f"h6 note {j}", "X") for j in range(n)])
    if project:
        p = projector.Projector(conn, ks, es, t)
        p.run()
        p.stop()


def _w_epoch(es, t):
    from wp import families as F
    rx = re.compile(rf"-w(\d+)$")
    return max(int(m.group(1)) for als in F._family_state(es, t, "events").values() for a in als for m in [rx.search(a)] if m)


def _line(out, tag):
    ln = next((x for x in out.splitlines() if x.startswith(tag + " ")), None)
    return json.loads(ln[len(tag) + 1:]) if ln else None


def _descriptor_check(es, keys):
    """C8: each key's role descriptors (read by the operator) name only its tenant and epoch."""
    from wp import families as F
    bad, n = [], 0
    for t, e, kid in keys:
        s, j = es.req("GET", "/_security/api_key", ok=None, id=kid)
        ks = j.get("api_keys", []) if s == 200 else []
        if not ks:
            bad.append(f"{kid}: not readable ({s})")
            continue
        n += 1
        names = [nm for rd in (ks[0].get("role_descriptors") or {}).values() for ip in rd.get("indices", []) for nm in ip["names"]]
        want = {F.write_alias(t, f, e) for f in F.FAMILIES} | {F.read_alias(t, f) for f in F.FAMILIES}
        if set(names) != want:
            bad.append(f"{kid}: names differ from its tenant and epoch")
    return {"checked": n, "bad": bad[:5], "n_bad": len(bad)}


def _pg_role_check():
    from wp import db
    out = {}
    c = db.broker()
    try:
        out["holder_fn"] = "callable" if c.execute("select wp_lease_holder_current('h6none', 'projector', 1, 'x')").fetchone()[0] is False else "unexpected"
    except Exception as e:  # noqa: BLE001
        out["holder_fn"] = getattr(e, "sqlstate", None) or type(e).__name__
    try:
        c.execute("select count(*) from leases").fetchone()
        out["select_leases"] = "allowed"
    except Exception as e:  # noqa: BLE001
        out["select_leases"] = getattr(e, "sqlstate", None) or type(e).__name__
    c.close()
    return out


def _found(hashes, blobs):
    hs = set(hashes)
    return sum(1 for b in blobs for tok in TOKEN.findall(b) if _sha(tok) in hs)


def _c12(es, D):
    from wp import config, families as F
    H2 = _h2()
    d = config.HOME / "run" / "c12"
    for f in ("phase1", "stopped", "phase2", "restarted"):
        (d / f).unlink(missing_ok=True)
    H2._docker(*H2._run_args("emlab2-wphard-app12", UIDS["app"], "app", ["-d"]), "-m", "exp.h_d6_broker", "c12",
               json.dumps({"D": D}))

    def wait(name, limit=300):
        t0 = time.time()
        while not (d / name).exists():
            if time.time() - t0 > limit:
                raise RuntimeError(f"c12 child did not reach {name}")
            time.sleep(0.5)
    wait("phase1")
    H2._docker("stop", "-t", "5", "emlab2-wphard-broker")
    (d / "stopped").write_text("1")
    wait("phase2")
    es.req("POST", f"/{F.base_index(D, 'events')}/_refresh", ok=None)
    docs_at_phase2 = es.count(F.base_index(D, "events"), {"match_all": {}})
    served = H2._docker("logs", "emlab2-wphard-broker", check=False).stdout.count("broker serving")
    H2._docker("start", "emlab2-wphard-broker")
    _wait_log("emlab2-wphard-broker", "broker serving", count=served + 1)
    (d / "restarted").write_text("1")
    H2._docker("wait", "emlab2-wphard-app12", timeout=600)
    out = H2._docker("logs", "emlab2-wphard-app12", check=False)
    H2._docker("rm", "-f", "emlab2-wphard-app12", check=False)
    res = _line(out.stdout, "H6C12") or {"child_failed": True, "stderr_tail": out.stderr[-300:]}
    res["docs_at_phase2"] = docs_at_phase2
    return res, _line(out.stdout, "H6C12HASH") or [], out.stdout


def once(arm, i):
    from exp.util import env, tenant, es_count_live
    from wp import config
    H2 = _h2()
    conn, ks, es = env()
    A, Bt, C, D = (tenant(f"h6{x}") for x in "abcd")
    _project(conn, ks, es, A)
    _project(conn, ks, es, Bt)
    eb = _w_epoch(es, Bt)
    if arm == "fenced":
        _project(conn, ks, es, C, project=False)
    args = json.dumps({"A": A, "B": Bt, "C": C, "eb": eb})
    extra = ["--rm", "-v", f"{config.SECRETS}:/operator/secrets:ro"] if arm == "fenced" else ["--rm"]
    cp = H2._docker(*H2._run_args("emlab2-wphard-app", UIDS["app"], "app", extra), "-m", "exp.h_d6_broker", "child", arm, args,
                    check=False, timeout=1800)
    res = _line(cp.stdout, "H6CHILD") or {"child_failed": cp.returncode, "stderr_tail": cp.stderr[-400:]}
    hashes = _line(cp.stdout, "H6CHILDHASH") or []
    res.update(run=i, tenants=[A, Bt, C, D], eb=eb)
    if arm == "fenced":
        res["C7_events_in_read_alias"] = es_count_live(es, C, "events", {"bool": {"must_not": [{"term": {"kind": "h6probe"}}]}})
        res["C8_descriptors"] = _descriptor_check(es, (res.get("C8") or {}).get("keys", []))
        res["C10"] = _pg_role_check()
        pc = H2._docker(*H2._run_args("emlab2-wphard-peer", UIDS["verifier"], "app", ["--rm"]), "-m", "exp.h_d6_broker", "peer", A,
                        check=False, timeout=300)
        res["C11"] = next((x[7:] for x in pc.stdout.splitlines() if x.startswith("H6PEER ")), f"error rc={pc.returncode}")
        c12, h12, out12 = _c12(es, D)
        res["C12"] = c12
        blog = H2._docker("logs", "emlab2-wphard-broker", check=False)
        blobs = [cp.stdout.encode(), out12.encode(), blog.stdout.encode(), blog.stderr.encode(),
                 json.dumps(res, sort_keys=True).encode()]
        res["C13"] = {"key_hashes": len(set(hashes) | set(h12)), "values_found": _found(set(hashes) | set(h12), blobs)}
    return res


def _st(x):
    return x[0] if isinstance(x, list) and x else None


def judge(arm, x, wit):
    """(why, invalid): reasons a case did not hold, and INVALID-REQUEST cases (setup rejected before the behaviour)."""
    why, inv = [], dict(x.get("invalid") or {})
    if x.get("child_failed") is not None:
        return [f"child failed: {x.get('stderr_tail', '')[-120:]}"], inv
    ok = lambda r: _st(r) in (200, 201)                      # noqa: E731
    if arm == "control":
        acc = [k for k, r in {**(x.get("C1") or {}), **(x.get("C4") or {})}.items() if ok(r)]
        return ([] if acc else ["control accepted no C1 or C4 request (cannot detect)"]), inv
    if not ok(x.get("own_write")):
        inv["own_write"] = f"own write {x.get('own_write')}: refusals would be vacuous"
    for c in ("C1", "C2", "C3", "C4"):
        for k, r in (x.get(c) or {}).items():
            if ok(r):
                why.append(f"{c} {k} accepted")
        if c not in x and not inv:
            why.append(f"{c} missing")
    c5 = x.get("C5") or {}
    if c5 and not ok(c5.get("h2_write")):
        inv["C5_h2_write"] = f"h2 write {c5.get('h2_write')}"
    if c5.get("stale_accepted", 1) != 0:
        why.append(f"C5 stale accepted {c5.get('stale_accepted')}")
    for k, v in (x.get("C6") or {}).items():
        if not str(v).startswith("Refused"):
            why.append(f"C6 {k}: {v}")
    c7 = x.get("C7") or {}
    if not (c7.get("p1_step_after_takeover") == -1 and c7.get("p1_written_after") == c7.get("p1_written_before")
            and c7.get("p2_done") is True and x.get("C7_events_in_read_alias") == 5):
        why.append(f"C7 {json.dumps({k: c7.get(k) for k in ('p1_step_after_takeover', 'p1_written_before', 'p1_written_after', 'p2_done')})} read_alias={x.get('C7_events_in_read_alias')}")
    c8, d8 = x.get("C8") or {}, x.get("C8_descriptors") or {}
    if c8.get("n_errors") or d8.get("n_bad") or d8.get("checked") != 2 * N_PAIRS:
        why.append(f"C8 errors {c8.get('n_errors')} bad {d8.get('n_bad')} checked {d8.get('checked')}")
    for n, st in (x.get("files") or {}).items():
        if wit.get(n) != "yes":
            inv[f"C9 {n}"] = "no existence witness"
        elif st != "EACCES":
            why.append(f"C9 {n}: {st}")
    c10 = x.get("C10") or {}
    if c10.get("holder_fn") != "callable" or c10.get("select_leases") != "42501":
        why.append(f"C10 {c10}")
    if "peer_refused" not in str(x.get("C11")):
        why.append(f"C11 {x.get('C11')}")
    c12 = x.get("C12") or {}
    inv.update({f"C12 {k}": v for k, v in (c12.get("invalid") or {}).items()})
    if c12.get("b_projector_start") == "started" or c12.get("c_stale_accepted", 1) != 0:
        why.append(f"C12 b={c12.get('b_projector_start')} c_accepted={c12.get('c_stale_accepted')}")
    if c12 and not ok(c12.get("a_window_write")):
        why.append(f"C12 UNEXPECTED: the window write was refused {c12.get('a_window_write')}")
    c13 = x.get("C13") or {}
    if c13.get("values_found", 1) != 0 or not c13.get("key_hashes"):
        why.append(f"C13 {c13}")
    return why, inv


def main(r=5):
    from exp.common import write_result
    from wp import es_keys, config
    res = {"R": r, "uids": UIDS, "n_stale": N_STALE, "n_pairs": N_PAIRS,
           "limits": ["one Elasticsearch node", "docker access on the host is root-equivalent: separation of service "
                      "identities on one host, not host isolation", "the broker trusts every allowed peer uid for every "
                      "tenant (tenant authority: design path item 2)", "holder strings are trusted within one service "
                      "identity", "a key already issued writes until the next CAS or its 1 h expiry (C12 a)",
                      "the operator directory is mounted into the app container for this test only"],
           "cleanup": "broker-minted keys are not invalidated by this experiment; they expire after 1 h"}
    res["broker_user"] = es_keys.create_broker()
    if not (config.SECRETS / "es_api_key.projector.json").exists():
        res["role_keys_created"] = sorted(es_keys.create())
    try:
        for arm in ("fenced", "control"):
            res[f"{arm}_files"] = prepare(arm)
            wit = res[f"{arm}_witness"] = witness() if arm == "fenced" else {}
            if arm == "fenced":
                start_services()
            rows = [once(arm, i) for i in range(r)]
            for x in rows:
                x["why"], x["invalid_request"] = judge(arm, x, wit)
                # control: the H6 rule is acceptance of a C1 or C4 request only (H6a)
                x["holds" if arm == "fenced" else "violated"] = not x["why"] and (arm == "control" or not x["invalid_request"])
            res[arm] = rows
            stop_services()
    finally:
        stop_services()
        _h2()._root("rm -rf /h/svc /h/run")
    f_ok = sum(x["holds"] for x in res["fenced"])
    c_ok = sum(x["violated"] for x in res["control"])
    inv = sorted({k for x in res["fenced"] for k in x["invalid_request"]})
    res["verdict"] = {"fenced_holds": f"{f_ok}/{r}", "control_violated": f"{c_ok}/{r}", "invalid_request_cases": inv,
                      "void": c_ok < r, "feasible": f_ok == r and c_ok == r and not inv}
    print("h_d6_broker", json.dumps(res["verdict"]), flush=True)
    write_result("h_d6_broker", res)
    return 0 if res["verdict"]["feasible"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        (child_fenced if sys.argv[2] == "fenced" else child_control)(json.loads(sys.argv[3]))
    elif sys.argv[1:2] == ["c12"]:
        child_c12(json.loads(sys.argv[2]))
    elif sys.argv[1:2] == ["peer"]:
        peer(sys.argv[2])
    else:
        from exp import stopsig
        stopsig.install()                       # review D2: a stop runs the finally cleanup
        sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
