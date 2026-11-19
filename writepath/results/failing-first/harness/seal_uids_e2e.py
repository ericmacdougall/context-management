"""Seal authority under its OWN OS uid (the H2 container pattern; owner a5bed869; UNSTAMPED, not a stamped result).
No sudo: this stream's own containers (ubuntu:24.04, host /usr and the venv read-only, host network, 512 MiB, label
emlab2.stream=wphard), as exp/h_d2_uids.py and exp/h_d6_broker.py do. Docker access on the host is root-equivalent,
so this is separation of service identities on one host, not host isolation (stated in the result).
  emlab2-wphard-keysvc  uid 20003  /svc/keysvc (pgpass.wp_kms)              wp.keysvc serve, allows 20001 and 20005
  emlab2-wphard-sealer  uid 20005  /svc/sealer (seal_key, pgpass.wp_sealer)  wp.sealer serve, allows 20001 only
  emlab2-wphard-app     uid 20001  /svc/app (pgpass.wp_app only)             one-shot child, WP_SEALER and WP_KEYSVC set
Cases (the child prints one JSON line; key values never enter it):
  K1  the app uid cannot open /svc/sealer/seal_key or /svc/sealer/pgpass.wp_sealer (EACCES)
  K2  with WP_SEALER unset the app has no seal (no key file of its own): SealUnavailable, nothing stored
  K3  through the socket: an append is accepted; its row carries the authority's key id and a 64-hex MAC
  K4  the F14 plant through wp_app (valid lease, digest, matcher and scan version; no attestation): WPS40
  K5  a forged attestation (64 hex digits under the authority's key id, not its MAC): WPS41
  K6  the sealer container stopped: the append raises SealUnavailable and nothing is stored
  K7  a peer with another uid (20002) asking the sealer: refused
usage (from a pinned tree, WP_HOME set, after wp.setup): python results/failing-first/harness/seal_uids_e2e.py"""
import hashlib
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
UIDS = {"app": 20001, "peer": 20002, "keysvc": 20003, "sealer": 20005}
KS_SOCK, S_SOCK = "/run/wphard/keysvc.sock", "/run/wphard/s/sealer.sock"
CHILD = "/src/results/failing-first/harness/seal_uids_e2e.py"


def _err(e):
    return getattr(e, "sqlstate", None) or type(e).__name__


# ------------------------------------------------------------------ inside the app container (uid 20001)
def child(phase, t):
    from exp.util import msg, session
    from wp import db, keysvc, log, sealer
    res = {"uid": os.getuid(), "phase": phase}
    conn = db.connect(application_name="seal-uids-app")
    ks = keysvc.key_store()
    s = session(conn, t, sid="su" + phase)
    n0 = conn.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0]
    if phase == "up":
        for f in ("seal_key", "pgpass.wp_sealer"):
            try:
                open(f"/svc/sealer/{f}", "rb").close()
                res[f"K1_{f}"] = "READABLE"
            except OSError as e:
                res[f"K1_{f}"] = type(e).__name__ + (f" errno={e.errno}" if e.errno else "")
        sock = os.environ.pop("WP_SEALER")
        try:
            log.append(conn, ks, s, [msg(0, "no authority note", "X")])
            res["K2"] = "stored"
        except sealer.SealUnavailable:
            res["K2"] = "SealUnavailable"
        os.environ["WP_SEALER"] = sock
        r = log.append(conn, ks, s, [msg(1, "socket note", "X")])
        row = conn.execute("select seal_key_id, seal_mac ~ '^[0-9a-f]{64}$', content_digest, mention_scan_upto, mention_matcher, "
                           "secret_scan, lease_epoch from events where tenant_id=%s and event_id=%s", (t, r["accepted"][0])).fetchone() \
            if r["accepted"] else None
        res["K3"] = {"accepted": len(r["accepted"]), "key_id_present": bool(row and row[0]), "mac_hex64": bool(row and row[1])}
        if row:
            kid, _, dg, scan, mid, sv, ep = row
            ch = conn.execute("select next_seq, last_hash from chains where tenant_id=%s and session_id=%s", (t, "su" + phase)).fetchone()
            base = dict(tenant_id=t, session_id="su" + phase, seq=ch[0], event_id="plant-" + phase, idem_key="plant-" + phase, req_hash="rh",
                        kind="message", subject_id="X", actor="user", trust_origin="user_typed", content_enc=b"x" * 40,
                        content_digest=hashlib.sha256(b"x" * 40).hexdigest(), mentions=[], mention_scan_upto=scan,
                        mention_matcher=mid, secret_scan=sv, prev_hash=ch[1], hash="h", lease_scope="session:su" + phase,
                        lease_epoch=ep)
            for case, extra in (("K4", {}), ("K5", dict(seal_key_id=kid, seal_mac="ab" * 32, seal_batch="b", seal_index=0, seal_count=1))):
                row2 = dict(base, **extra)
                try:
                    with conn.transaction():
                        conn.execute("insert into events(%s, ts_event) values (%s, now())" % (", ".join(row2), ", ".join(["%s"] * len(row2))),
                                     list(row2.values()))
                    res[case] = "ok"
                except Exception as e:  # noqa: BLE001
                    res[case] = _err(e)
    else:
        try:
            log.append(conn, ks, s, [msg(2, "authority down note", "X")])
            res["K6"] = "stored"
        except sealer.SealUnavailable:
            res["K6"] = "SealUnavailable"
    res["stored_delta"] = conn.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0] - n0
    print("SEALUIDS " + json.dumps(res, sort_keys=True), flush=True)


def peer():
    from wp import sealer
    try:
        sealer.Client(S_SOCK).seal("x", "session:x", 1, "b", [{"event_id": "e", "subject_id": "X", "content": "hi"}])
        print("SEALPEER issued", flush=True)
    except Exception as e:  # noqa: BLE001
        print("SEALPEER " + type(e).__name__ + " " + str(e)[:60], flush=True)


# ------------------------------------------------------------------ orchestrator (operator)
def main():
    import exp.h_d2_uids as H2
    from exp.util import tenant
    from wp import config
    ucd = os.environ["WP_UCD_DIR"]
    extra_env = ["-v", f"{ucd}:/ucd:ro", "-e", "WP_UCD_DIR=/ucd"]
    names = [f"emlab2-wphard-{n}" for n in ("keysvc", "sealer", "app", "peer")]

    def stop_all():
        for n in names:
            H2._docker("rm", "-f", n, check=False)

    def wait_log(name, text):
        t0 = time.time()
        while text not in H2._docker("logs", name, check=False).stdout:
            if time.time() - t0 > 60:
                raise RuntimeError(f"{name} did not start: " + H2._docker("logs", "--tail", "3", name, check=False).stderr[-300:])
            time.sleep(1)

    def run_child(name, uid, sdir, args, env=()):
        return H2._docker(*H2._run_args(name, uid, sdir, ["--rm", *extra_env, *env]), CHILD, *args, check=False, timeout=600)

    have = set(os.listdir(config.SECRETS))
    need = {"app": (UIDS["app"], ["pgpass.wp_app"]), "keysvc": (UIDS["keysvc"], ["pgpass.wp_kms"]),
            "sealer": (UIDS["sealer"], ["seal_key", "pgpass.wp_sealer"]), "peer": (UIDS["peer"], [])}
    missing = [f for _, fs in need.values() for f in fs if f not in have]
    if missing:
        print("seal_uids_e2e SETUP missing secret files", missing)
        return 2
    # the named PREP helper of amendment H2a (secrets read-only, only svc and run writable)
    lines = H2.layout(UIDS["keysvc"]) + [f"install -d -o {UIDS['sealer']} -g {UIDS['sealer']} -m 755 /h/run/s"]
    for d, (uid, files) in need.items():
        lines.append(f"install -d -o {uid} -g {uid} -m 700 /h/svc/{d}")
        lines += [f"install -o {uid} -g {uid} -m 600 /h/secrets/{f} /h/svc/{d}/{f}" for f in files]
    out = {"limits": ["docker access on the host is root-equivalent: separation of service identities on one host, not host "
                      "isolation", "one host, one Postgres", "the authority trusts every allowed peer uid for every tenant"],
           "uids": UIDS}
    stop_all()
    try:
        H2._prep("\n".join(lines))
        H2._docker(*H2._run_args(names[0], UIDS["keysvc"], "keysvc",
                                 ["-d", *extra_env, "-e", f"WP_KEYSVC_ALLOW_UIDS={UIDS['app']},{UIDS['sealer']}"]),
                   "-m", "wp.keysvc", "serve", KS_SOCK)
        wait_log(names[0], "keysvc serving")
        H2._docker(*H2._run_args(names[1], UIDS["sealer"], "sealer",
                                 ["-d", *extra_env, "-e", f"WP_SEALER_ALLOW_UIDS={UIDS['app']}", "-e", f"WP_KEYSVC={KS_SOCK}"]),
                   "-m", "wp.sealer", "serve", S_SOCK)
        wait_log(names[1], "sealer serving")
        t = tenant("su")
        env = ["-e", f"WP_SEALER={S_SOCK}", "-e", f"WP_KEYSVC={KS_SOCK}"]
        c = run_child(names[2], UIDS["app"], "app", ["child", "up", t], env)
        up = next((json.loads(x[9:]) for x in c.stdout.splitlines() if x.startswith("SEALUIDS ")), {"child_failed": c.stderr[-400:]})
        p = run_child(names[3], UIDS["peer"], "peer", ["peer"])
        out["K7"] = next((x[9:] for x in p.stdout.splitlines() if x.startswith("SEALPEER ")), "error " + p.stderr[-200:])
        H2._docker("rm", "-f", names[1], check=False)
        c = run_child(names[2], UIDS["app"], "app", ["child", "down", t], env)
        down = next((json.loads(x[9:]) for x in c.stdout.splitlines() if x.startswith("SEALUIDS ")), {"child_failed": c.stderr[-400:]})
        out["up"], out["down"] = up, down
    finally:
        stop_all()
        H2._prep(H2.CLEAR)
    ok = (up.get("K1_seal_key", "").startswith("PermissionError") and up.get("K1_pgpass.wp_sealer", "").startswith("PermissionError")
          and up.get("K2") == "SealUnavailable" and up.get("K3") == {"accepted": 1, "key_id_present": True, "mac_hex64": True}
          and up.get("K4") == "WPS40" and up.get("K5") == "WPS41" and up.get("stored_delta") == 1
          and down.get("K6") == "SealUnavailable" and down.get("stored_delta") == 0 and "SealUnavailable" in out["K7"])
    out["verdict"] = "PASS" if ok else "FAIL"
    print("seal_uids_e2e", json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ["peer"]:
        peer()
    else:
        from exp import stopsig
        stopsig.install()                       # review D2: a stop runs the finally cleanup
        sys.exit(main())
