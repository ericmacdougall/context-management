"""Seal authority over its socket, end to end, on the lab host (owner a5bed869; UNSTAMPED smoke, not a stamped result).
Runs `python -m wp.sealer serve` as a child process of THIS uid, sets WP_SEALER, and appends through wp.log.append:
  E1  allowed peer: the append is accepted and the stored row carries the authority's key id and a 64-hex MAC
  E2  authority stopped: the append raises SealUnavailable and the tenant's event count is unchanged
  E3  authority up but this uid not in WP_SEALER_ALLOW_UIDS: SealUnavailable (peer refused), nothing stored
LIMIT (stated, not hidden): the authority runs under the SAME OS uid as the app here, so this shows the socket path and
the store's acceptance, NOT key isolation (the app's uid can read the key file). Key isolation needs distinct uids (the
H2 container pattern), which this smoke does not build.
usage (from a pinned tree, WP_HOME set): python results/failing-first/harness/seal_socket_e2e.py"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from exp.util import env, msg, session, tenant  # noqa: E402
from wp import config, log, sealer  # noqa: E402


def serve(sock, allow):
    if sock.exists():
        sock.unlink()
    p = subprocess.Popen([sys.executable, "-m", "wp.sealer", "serve", str(sock)], cwd=str(pathlib.Path(__file__).resolve().parents[3]),
                         env=dict(os.environ, WP_SEALER_ALLOW_UIDS=str(allow)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while not sock.exists():
        if p.poll() is not None or time.time() - t0 > 30:
            raise RuntimeError("sealer did not start: " + (p.stdout.read()[-300:] if p.poll() is not None else "timeout"))
        time.sleep(0.2)
    return p


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def count(conn, t):
    return conn.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0]


def main():
    d = config.HOME / "state" / "a5b-seal"
    d.mkdir(parents=True, exist_ok=True)
    sock = d / "sealer.sock"
    conn, ks, _ = env()
    t = tenant("sl")
    s = session(conn, t)
    out = {"same_uid_limit": True}
    os.environ["WP_SEALER"] = str(sock)
    p = serve(sock, os.getuid())
    try:
        r = log.append(conn, ks, s, [msg(0, "socket path note", "X")])
        row = conn.execute("select seal_key_id is not null, seal_mac ~ '^[0-9a-f]{64}$' from events where tenant_id=%s and event_id=%s",
                           (t, r["accepted"][0])).fetchone() if r["accepted"] else None
        out["E1"] = {"accepted": len(r["accepted"]), "row_attested": list(row) if row else None}
    finally:
        stop(p)
    n0 = count(conn, t)
    try:
        log.append(conn, ks, s, [msg(1, "authority down note", "X")])
        out["E2"] = {"raised": None}
    except sealer.SealUnavailable:
        out["E2"] = {"raised": "SealUnavailable"}
    out["E2"]["stored"] = count(conn, t) - n0
    p = serve(sock, os.getuid() + 1)
    try:
        try:
            log.append(conn, ks, s, [msg(2, "refused peer note", "X")])
            out["E3"] = {"raised": None}
        except sealer.SealUnavailable:
            out["E3"] = {"raised": "SealUnavailable"}
        out["E3"]["stored"] = count(conn, t) - n0
    finally:
        stop(p)
        if sock.exists():
            sock.unlink()
    ok = (out["E1"]["accepted"] == 1 and out["E1"]["row_attested"] == [True, True] and out["E2"] == {"raised": "SealUnavailable", "stored": 0}
          and out["E3"] == {"raised": "SealUnavailable", "stored": 0})
    print("seal_socket_e2e", json.dumps(out, sort_keys=True), "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
