"""Delta review D2, as far as one host and one OS user allow (THRESHOLDS amendment 12).

Separate credential directories (WP_SECRETS_DIR, 0700, files 0600 copied from the role's own file, never printed):
  app dir       pgpass.wp_app, pgpass.wp_kms   (the lab runner also drives the key-service stand-in; see limits)
  verifier dir  pgpass.wp_verifier
  shared dir    all three (the control: the 963e597 deployment, one credential store for every role)
The verifier runs as its own OS process (python -m wp.verifier_service) with the verifier dir.
Per run, one tenant; X has 5 events, projected. The APPLICATION runs as a child process with its credential dir:
  fenced  (app dir)    a) connecting as wp_verifier fails; b) wp_erasure_complete as wp_app is refused;
                       c) a dishonest runner (placeholders skipped) that tries to complete with fabricated zero ES
                          counts cannot: no credential, so the job is not done; d) the honest runner with
                          verifier="external" reaches done through the separate process.
  control (shared dir) the same dishonest runner completes the job with fabricated zeros: done, while X's content is
                       still searchable in Elasticsearch.
PASS (R=5): fenced a, b, c hold and d is done 5/5; control reports done with ES residue 5/5.
Limits, stated: same OS user, so file permissions do not separate the processes; the runner holds the key-service
credential; separate service identities (users or hosts) are a deployment requirement.
usage: python -m exp.d2_verifier_process [R]"""
import json
import os
import pathlib
import subprocess
import sys
import time

from exp.common import write_result
from exp.util import tenant, msg, session, env, es_count_all

PY = sys.executable


def _dirs():
    from wp import config
    base = config.HOME / "secrets-d2"
    sets = {"app": ["wp_app", "wp_kms"], "verifier": ["wp_verifier"], "shared": ["wp_app", "wp_kms", "wp_verifier"]}
    out = {}
    for name, roles in sets.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700); os.chmod(d, 0o700)
        for f in d.iterdir():
            f.unlink()
        for r in roles:
            src = pathlib.Path(config.pgpass(r))
            fd = os.open(d / src.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, src.read_bytes())
            finally:
                os.close(fd)
        out[name] = str(d)
    return out


def child(t):
    """Runs inside the APPLICATION process (its WP_SECRETS_DIR decides what it can reach). Prints one JSON line."""
    import psycopg
    from wp import db, erasure, registry
    conn, ks, es = env()
    res = {}
    try:
        db.verifier().close(); res["connect_as_verifier"] = True
    except Exception as e:  # noqa: BLE001
        res["connect_as_verifier"] = False; res["connect_error"] = type(e).__name__
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
    res["dishonest_done"] = rec["done"]
    if not rec["done"] and os.environ.get("D2_HONEST") == "1":
        rec2 = erasure.run(conn, ks, es, jid, verifier="external", physical="wait")
        res["honest_external_done"], res["honest_error"] = rec2["done"], rec2["error_type"]
    print("D2CHILD " + json.dumps(res), flush=True)


def once(arm, dirs):
    from wp import log, projector
    conn, ks, es = env()
    t = tenant("d2")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"X secret note d2mark{i}", "X") for i in range(5)])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    envc = dict(os.environ, WP_SECRETS_DIR=dirs["app" if arm == "fenced" else "shared"], D2_HONEST="1" if arm == "fenced" else "0")
    cp = subprocess.run([PY, "-m", "exp.d2_verifier_process", "child", t], env=envc, capture_output=True, text=True, timeout=900)
    line = next((ln for ln in cp.stdout.splitlines() if ln.startswith("D2CHILD ")), None)
    res = json.loads(line[8:]) if line else {"child_failed": cp.returncode, "stderr_tail": cp.stderr[-300:]}
    res["es_x_content_docs"] = es_count_all(es, t, {"bool": {"filter": [{"term": {"subject_id": "X"}}], "must_not": [{"term": {"erased": True}}]}})
    return res


def main(r):
    dirs = _dirs()
    vs = subprocess.Popen([PY, "-m", "wp.verifier_service", "1.0", "2.0"], env=dict(os.environ, WP_SECRETS_DIR=dirs["verifier"]),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        res = {"R": r, "verifier_pid_separate": vs.pid != os.getpid(), "fenced": [once("fenced", dirs) for _ in range(r)],
               "control": [once("control", dirs) for _ in range(r)]}
    finally:
        vs.terminate(); vs.wait(30)
    why = []
    for i, x in enumerate(res["fenced"]):
        ok = (x.get("connect_as_verifier") is False and x.get("complete_as_app") not in (None, "accepted")
              and x.get("dishonest_done") is False and x.get("honest_external_done") is True)
        if not ok:
            why.append(f"fenced[{i}] {x}")
    for i, x in enumerate(res["control"]):
        if not (x.get("connect_as_verifier") is True and x.get("dishonest_done") is True and x.get("es_x_content_docs", 0) > 0):
            why.append(f"control[{i}] did not fabricate a done with ES residue: {x}")
    res["verdict"] = {"pass": not why, "why": why}
    print("d2_verifier_process", res["verdict"], flush=True)
    write_result("d2_verifier_process", res)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "child":
        child(sys.argv[2])
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
