"""Amendment H2 (D2): separate service identities, proved with distinct uids in this stream's own containers.

The orchestrator (this process, the operator, uid of the host user) prepares one credential directory per service
under WP_HOME/svc through a named root helper container, emlab2-wphard-h2prep (copy, chown, chmod; values are never
printed; it sees the operator's secrets read-only and may write only WP_HOME/svc and WP_HOME/run; a second named
helper, emlab2-wphard-h2witness, reads svc and secrets read-only for the existence witness) and runs:
  emlab2-wphard-keysvc    uid 20003  WP_SECRETS_DIR=/svc/keysvc   (pgpass.wp_kms)          python -m wp.keysvc serve
  emlab2-wphard-verifier  uid 20002  WP_SECRETS_DIR=/svc/verifier (pgpass.wp_verifier, its ES key if any)
                                                                                             python -m wp.verifier_service
  emlab2-wphard-app       uid 20001  WP_SECRETS_DIR=/svc/app      (pgpass.wp_app, projector and eraser ES keys if any)
                                     one-shot per run: python -m exp.h_d2_uids child <tenant> <arm>
Every container: ubuntu:24.04 with the host's /usr and this stream's venv read-only (no image is built), host
network (the stores listen on loopback), 512 MiB, label emlab2.stream=wphard. The whole /svc tree is mounted into
every container read-only: only the kernel's permission check on each 0700 directory separates the services. The
operator's own directory (admin pgpass, `elastic` password, admin ES key) is mounted read-only into the APP container
for this test only, to show the kernel refuses the app uid; a deployment mounts it nowhere.
Arms:
  fenced   the layout above.
  control  ONE uid (20001) and ONE shared directory holding every service credential (the 963e597 deployment), for
           all three containers.
Per run (fresh tenant; X has 5 events, projected by the operator) the app child records: each probe file opened
(errno name, or readable); connects as wp_verifier, wp_kms and the admin (each fails or succeeds); wp_erasure_complete
as wp_app; a dishonest runner (placeholders skipped, fabricated zero counts through wp_verifier if it can) and, in the
fenced arm, the honest runner with verifier="external". The orchestrator records: a witness that each probe file
exists (root helper), the job state, X's non-placeholder documents in ES, and whether the VERIFIER's uid is served by
the key service.
Limits (stated in the result): docker access on the host is root-equivalent, so this shows separation of service
identities on one host, not host isolation; the app still receives subject key bytes (app-side encryption).
usage: python -m exp.h_d2_uids [R]"""
import json
import os
import pathlib
import subprocess
import sys
import time

UIDS = {"app": 20001, "verifier": 20002, "keysvc": 20003}
IMAGE = "ubuntu:24.04"
LABEL = "emlab2.stream=wphard"
SOCK = "/run/wphard/keysvc.sock"
PROBE_FILES = {"verifier_pgpass": "/svc/{verifier}/pgpass.wp_verifier", "kms_pgpass": "/svc/{keysvc}/pgpass.wp_kms",
               "admin_pgpass": "/operator/secrets/pgpass", "elastic_pw_file": "/operator/secrets/es_elastic_password",
               "admin_es_key": "/operator/secrets/es_api_key.json"}


# ------------------------------------------------------------------ inside the application container
def child(t, arm):
    import errno
    import psycopg
    from wp import config, db, erasure, keysvc, registry
    from wp.es import ES
    dirs = {"verifier": "shared", "keysvc": "shared"} if arm == "control" else {"verifier": "verifier", "keysvc": "keysvc"}
    res = {"uid": os.getuid(), "files": {}, "connect": {}}
    for name, p in PROBE_FILES.items():
        p = p.format(**dirs)
        try:
            with open(p, "rb") as f:
                f.read(1)
            res["files"][name] = "readable"
        except OSError as e:
            res["files"][name] = errno.errorcode.get(e.errno, str(e.errno))
    for who, fn in (("wp_verifier", lambda: db.verifier()), ("wp_kms", lambda: db.keys_connect()), ("admin", lambda: db.admin())):
        try:
            fn().close()
            res["connect"][who] = "connected"
        except Exception as e:  # noqa: BLE001
            res["connect"][who] = type(e).__name__
    conn = db.connect(application_name="h-d2-app")
    ks = keysvc.key_store()
    res["key_store"] = ks.describe().get("transport", "in-process")
    es = ES(role="eraser") if any((config.SECRETS / f"es_api_key.{r}.json").exists() for r in config.ES_ROLES) else ES()
    zeros = {s.name: 0 for s in registry.verifiable() if s.kind != "pg"}

    class Fabricating:
        """A dishonest runner's verifier: claims zero residue in every non-Postgres store, as wp_verifier if it can."""
        def complete(self, jid, expunge=True, wait_s=None):
            try:
                c = db.verifier()
            except Exception as e:  # noqa: BLE001
                return {"done": False, "receipt_state": "residue", "fabricate_error": type(e).__name__}
            return c.execute("select wp_erasure_complete(%s, %s, %s)", (jid, json.dumps(zeros), "fabricated")).fetchone()[0]

    jid = erasure.request(conn, t, "X")
    try:
        conn.execute("select wp_erasure_complete(%s, %s, %s)", (jid, json.dumps(zeros), "as app"))
        res["complete_as_app"] = "accepted"
    except psycopg.Error as e:
        res["complete_as_app"] = getattr(e, "sqlstate", None) or type(e).__name__
    rec = erasure.run(conn, ks, es, jid, placeholders=False, verifier=Fabricating(), physical="followup")
    res["dishonest_done"], res["dishonest_error"] = rec["done"], rec["error_type"]
    if arm == "fenced" and not rec["done"]:
        rec2 = erasure.run(conn, ks, es, jid, verifier="external", physical="wait")
        res["honest_external_done"], res["honest_error"] = rec2["done"], rec2["error_type"]
    print("H2CHILD " + json.dumps(res), flush=True)


def keysvc_probe():
    """Inside the VERIFIER container: is this uid served by the key service?"""
    from wp import keysvc
    try:
        c = keysvc.Client(SOCK)
        c.keys("h2probe", ["X"])
        print("H2KS served", flush=True)
    except keysvc.ServiceError as e:
        # review B6: only the protocol's own refusal counts; any other failure is an outage, never a refusal
        print("H2KS " + ("peer_refused" if str(e) == "peer_refused" else "service_error " + type(e).__name__), flush=True)
    except Exception as e:  # noqa: BLE001
        print("H2KS unavailable " + type(e).__name__, flush=True)


# ------------------------------------------------------------------ orchestrator (operator)
def _docker(*a, check=True, timeout=600):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check, timeout=timeout)


# Root helpers (main's h_d2 decision for the gate's service-container rule): two NAMED one-shot containers, no network,
# this stream's label. Neither sees WP_HOME as a whole:
#   PREP     emlab2-wphard-h2prep     WP_HOME/secrets read-only at /h/secrets; WP_HOME/svc and WP_HOME/run read-write
#   WITNESS  emlab2-wphard-h2witness  WP_HOME/svc and WP_HOME/secrets, both read-only
PREP, WITNESS = "emlab2-wphard-h2prep", "emlab2-wphard-h2witness"


def _helper_args(name, mounts):
    """docker run arguments of a named root helper; `mounts` is [(host path, container path, "ro" | "rw")]."""
    vols = [a for h, c, mode in mounts for a in ("-v", f"{h}:{c}:{mode}")]
    return ["run", "--rm", "--name", name, "--network", "none", "--label", LABEL, *vols, IMAGE, "sh", "-c"]


def _prep(script):
    """PREP helper: may change only WP_HOME/svc and WP_HOME/run; reads the operator's secrets read-only."""
    from wp import config
    for d in ("svc", "run"):                  # bind sources made by the operator, so docker never creates them
        (config.HOME / d).mkdir(exist_ok=True)
    _docker("rm", "-f", PREP, check=False)    # a leftover of an interrupted run
    return _docker(*_helper_args(PREP, [(config.SECRETS, "/h/secrets", "ro"), (config.HOME / "svc", "/h/svc", "rw"),
                                        (config.HOME / "run", "/h/run", "rw")]), script).stdout


def _witness(script):
    """WITNESS helper: reads WP_HOME/svc and WP_HOME/secrets, writes nothing. Prints only names and flags."""
    from wp import config
    _docker("rm", "-f", WITNESS, check=False)
    return _docker(*_helper_args(WITNESS, [(config.HOME / "svc", "/h/svc", "ro"), (config.SECRETS, "/h/secrets", "ro")]),
                   script).stdout


# /h/svc and /h/run are mount points inside PREP: emptied, never removed
CLEAR = "find /h/svc /h/run -mindepth 1 -delete"


def layout(run_uid):
    """The first lines of every PREP script (h_d2, h_d6_broker, seal_uids_e2e): empty svc and run, svc root-owned 755,
    run owned by `run_uid` 755. Callers append their per-service directories."""
    return ["set -e", CLEAR, "chown 0:0 /h/svc", "chmod 755 /h/svc", f"chown {run_uid}:{run_uid} /h/run", "chmod 755 /h/run"]


def prepare(arm):
    """Credential directories for an arm, copied from the operator's own files (never printed)."""
    from wp import config
    es_files = {r: f"es_api_key.{r}.json" for r in config.ES_ROLES}
    have = {f for f in os.listdir(config.SECRETS)}
    sets = ({"shared": (UIDS["app"], ["pgpass.wp_app", "pgpass.wp_verifier", "pgpass.wp_kms", *[es_files[r] for r in config.ES_ROLES]])}
            if arm == "control" else
            {"app": (UIDS["app"], ["pgpass.wp_app", es_files["projector"], es_files["eraser"]]),
             "verifier": (UIDS["verifier"], ["pgpass.wp_verifier", es_files["verifier"]]),
             "keysvc": (UIDS["keysvc"], ["pgpass.wp_kms"])})
    ks_uid = UIDS["app"] if arm == "control" else UIDS["keysvc"]
    lines = layout(ks_uid)
    for d, (uid, files) in sets.items():
        lines.append(f"install -d -o {uid} -g {uid} -m 700 /h/svc/{d}")
        for f in files:
            if f in have:
                lines.append(f"install -o {uid} -g {uid} -m 600 /h/secrets/{f} /h/svc/{d}/{f}")
    _prep("\n".join(lines))
    return {d: [f for f in files if f in have] for d, (_, files) in sets.items()}


def witness(arm):
    """WITNESS helper: does each probe file exist and hold something? (yes/no only)"""
    dirs = {"verifier": "shared", "keysvc": "shared"} if arm == "control" else {"verifier": "verifier", "keysvc": "keysvc"}
    paths = {n: p.format(**dirs).replace("/svc/", "/h/svc/").replace("/operator/secrets/", "/h/secrets/") for n, p in PROBE_FILES.items()}
    out = _witness("; ".join(f"[ -s {p} ] && echo {n}=yes || echo {n}=no" for n, p in paths.items()))
    return dict(ln.split("=") for ln in out.split())


def _run_args(name, uid, secrets_dir, extra=()):
    from wp import config
    src = pathlib.Path(__file__).resolve().parents[1]
    return ["run", "--label", LABEL, "--network", "host", "-m", "512m", "--user", f"{uid}:{uid}",
            "-v", "/usr:/usr:ro", "-v", f"{config.HOME / 'venv'}:/venv:ro", "-v", f"{src}:/src:ro",
            "-v", f"{config.HOME / 'conf'}:/wp/conf:ro", "-v", f"{config.HOME / 'logs'}:/wp/logs:ro",
            "-v", f"{config.HOME / 'svc'}:/svc:ro", "-v", f"{config.HOME / 'run'}:/run/wphard",
            "-e", "WP_HOME=/wp", "-e", f"WP_SECRETS_DIR=/svc/{secrets_dir}", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp", "-w", "/src", *extra, "--name", name, IMAGE, "/venv/bin/python"]


def start_services(arm):
    shared = arm == "control"
    ks_uid = UIDS["app"] if shared else UIDS["keysvc"]
    v_uid = UIDS["app"] if shared else UIDS["verifier"]
    for n in ("keysvc", "verifier"):
        _docker("rm", "-f", f"emlab2-wphard-{n}", check=False)
    _docker(*_run_args("emlab2-wphard-keysvc", ks_uid, "shared" if shared else "keysvc",
                       ["-d", "-e", f"WP_KEYSVC_ALLOW_UIDS={UIDS['app']}"]), "-m", "wp.keysvc", "serve", SOCK)
    _docker(*_run_args("emlab2-wphard-verifier", v_uid, "shared" if shared else "verifier", ["-d"]), "-m", "wp.verifier_service", "1.0", "2.0")
    t0 = time.time()
    while "keysvc serving" not in _docker("logs", "emlab2-wphard-keysvc", check=False).stdout:
        if time.time() - t0 > 60:
            raise RuntimeError("key service did not start: " + _docker("logs", "--tail", "5", "emlab2-wphard-keysvc", check=False).stderr[-300:])
        time.sleep(1)


def stop_services():
    for n in ("keysvc", "verifier", "app"):
        _docker("rm", "-f", f"emlab2-wphard-{n}", check=False)


def once(arm, i):
    from exp.util import tenant, msg, session, env, es_count_all
    from wp import log, projector
    conn, ks, es = env()
    t = tenant("h2")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(j, f"X secret note h2mark{j}", "X") for j in range(5)])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    app_uid = UIDS["app"]
    extra = ["--rm", "-e", f"WP_KEYSVC={SOCK}"]
    if arm == "fenced":
        from wp import config
        extra += ["-v", f"{config.SECRETS}:/operator/secrets:ro"]
    cp = _docker(*_run_args(f"emlab2-wphard-app", app_uid, "shared" if arm == "control" else "app", extra),
                 "-m", "exp.h_d2_uids", "child", t, arm, check=False, timeout=900)
    line = next((ln for ln in cp.stdout.splitlines() if ln.startswith("H2CHILD ")), None)
    res = json.loads(line[8:]) if line else {"child_failed": cp.returncode, "stderr_tail": cp.stderr[-400:]}
    v = _docker("exec", "emlab2-wphard-verifier", "/venv/bin/python", "-m", "exp.h_d2_uids", "keysvc_probe", check=False)
    res["verifier_uid_keysvc"] = (v.stdout.strip().split(" ")[1] if v.stdout.startswith("H2KS") else f"error rc={v.returncode}")
    res["es_x_content_docs"] = es_count_all(es, t, {"bool": {"filter": [{"term": {"subject_id": "X"}}], "must_not": [{"term": {"erased": True}}]}})
    res["job_state"] = conn.execute("select state from erasure_jobs where tenant_id=%s and subject_id='X'", (t,)).fetchone()[0]
    res["run"], res["tenant"] = i, t
    return res


def judge(arm, x, wit):
    why = []
    if arm == "fenced":
        for n, st in x.get("files", {}).items():
            if wit.get(n) != "yes":
                why.append(f"{n}: no existence witness")
            if st != "EACCES":
                why.append(f"{n}: {st}")
        if not x.get("files"):
            why.append("no file probes")
        why += [f"connected as {w}" for w, st in x.get("connect", {}).items() if st == "connected"]
        if x.get("complete_as_app") in (None, "accepted"):
            why.append(f"complete_as_app {x.get('complete_as_app')}")
        if x.get("dishonest_done") is not False:
            why.append("dishonest runner reached done")
        if x.get("honest_external_done") is not True:
            why.append(f"honest external not done ({x.get('honest_error')})")
        if "separate key-service process" not in str(x.get("key_store")):
            why.append("app did not use the key service")
        if x.get("verifier_uid_keysvc") != "peer_refused":     # review B6: the exact protocol refusal
            why.append(f"verifier uid keysvc {x.get('verifier_uid_keysvc')}")
        if x.get("job_state") != "done":
            why.append(f"job {x.get('job_state')}")
    else:
        if x.get("files", {}).get("verifier_pgpass") != "readable":
            why.append("control: app could not read the verifier credential")
        if x.get("connect", {}).get("wp_verifier") != "connected":
            why.append("control: app could not connect as wp_verifier")
        if not (x.get("dishonest_done") is True and x.get("es_x_content_docs", 0) > 0):
            why.append("control: dishonest runner did not reach done with ES residue")
    return why


def main(r=5):
    from exp.common import write_result
    res = {"R": r, "uids": UIDS, "image": IMAGE,
           "limits": ["docker access on the host is root-equivalent: separation of service identities on one host, "
                      "not host isolation", "the app receives subject key bytes (app-side encryption)",
                      "the operator directory is mounted into the app container for this test only"]}
    try:
        for arm in ("fenced", "control"):
            res[f"{arm}_files"] = prepare(arm)
            res[f"{arm}_witness"] = wit = witness(arm)
            start_services(arm)
            rows = [once(arm, i) for i in range(r)]
            for x in rows:
                x["why"] = judge(arm, x, wit)
                x["holds" if arm == "fenced" else "violated"] = not x["why"]
            res[arm] = rows
            stop_services()
    finally:
        stop_services()
        _prep(CLEAR)
    f_ok = sum(x["holds"] for x in res["fenced"])
    c_ok = sum(x["violated"] for x in res["control"])
    res["verdict"] = {"fenced_holds": f"{f_ok}/{r}", "control_violated": f"{c_ok}/{r}", "pass": f_ok == r and c_ok == r}
    print("h_d2_uids", res["verdict"], flush=True)
    write_result("h_d2_uids", res)
    return 0 if res["verdict"]["pass"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ["keysvc_probe"]:
        keysvc_probe()
    else:
        from exp import stopsig
        stopsig.install()                       # review D2: a stop runs the finally cleanup
        sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
