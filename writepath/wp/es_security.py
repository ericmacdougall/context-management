"""Turn security on (or off) for THIS stream's own Elasticsearch node, container conf/es_container only (D6); the name
must start with config.OWN_PREFIX and the container's published port must be the conf es_url port.

enable: recreate the container from its own docker-inspect record (same image, ports, mounts, ulimits, memory) with
  xpack.security.enabled=true (HTTP without TLS on 127.0.0.1, single node), wait for it to answer 401, reset the
  `elastic` password inside the container with its stdout written straight to secrets/es_elastic_password (0600; the
  value never reaches a terminal, argv or the environment), then create the per-role keys (wp/es_keys.py).
disable: the same recreation with xpack.security.enabled=false (rollback).
Refuses any other container name. Data on the node is not preserved unless the container has a volume; the lab
recreates every index from Postgres on demand. usage: python -m wp.es_security enable|disable"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import config

NAME = config.ES_CONTAINER
if not NAME.startswith(config.OWN_PREFIX):          # config refuses this too; checked again before any docker rm
    raise SystemExit(f"refusing: {NAME!r} is not this stream's container")


def _inspect():
    j = json.loads(subprocess.run(["docker", "inspect", NAME], check=True, capture_output=True, text=True).stdout)[0]
    assert j["Name"].lstrip("/") == NAME and NAME.startswith(config.OWN_PREFIX)
    ports = {b["HostPort"] for bs in (j["HostConfig"].get("PortBindings") or {}).values() for b in bs or []}
    if str(config.ES_PORT) not in ports:
        raise SystemExit(f"refusing: {NAME} does not publish the conf es_url port")
    return j


def _run_args(j, security):
    hc, cfg = j["HostConfig"], j["Config"]
    args = ["docker", "run", "-d", "--name", NAME]
    for k, v in (cfg.get("Labels") or {}).items():     # keep the stream label (host/bootstrap.py checks it)
        if not k.startswith("org.opencontainers."):
            args += ["--label", f"{k}={v}"]
    for cport, binds in (hc.get("PortBindings") or {}).items():
        for b in binds or []:
            args += ["-p", f"{b.get('HostIp') or '127.0.0.1'}:{b['HostPort']}:{cport.split('/')[0]}"]
    for m in j.get("Mounts") or []:
        src = m["Name"] if m["Type"] == "volume" else m["Source"]
        args += ["-v", f"{src}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro")]
    for u in hc.get("Ulimits") or []:
        args += ["--ulimit", f"{u['Name']}={u['Soft']}:{u['Hard']}"]
    if hc.get("Memory"):
        args += ["-m", str(hc["Memory"])]
    env = [e for e in cfg.get("Env") or [] if not e.startswith(("xpack.security.", "ELASTIC_PASSWORD"))]
    env += [f"xpack.security.enabled={'true' if security else 'false'}"]
    if security:
        env += ["xpack.security.http.ssl.enabled=false", "xpack.security.transport.ssl.enabled=false"]
    for e in env:
        if e.split("=", 1)[0] in ("PATH", "ELASTIC_CONTAINER", "JAVA_HOME", "LANG", "LC_ALL"):
            continue
        args += ["-e", e]
    return args + [cfg["Image"]]


def _status():
    try:
        return urllib.request.urlopen(config.ES_URL, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return None


def _recreate(security):
    j = _inspect()
    args = _run_args(j, security)
    subprocess.run(["docker", "rm", "-f", NAME], check=True, capture_output=True)
    subprocess.run(args, check=True, capture_output=True)
    want = 401 if security else 200
    t0 = time.time()
    while _status() != want:
        if time.time() - t0 > 300:
            raise SystemExit(f"node did not answer {want} within 300 s")
        time.sleep(2)


def enable():
    _recreate(True)
    p = config.SECRETS / "es_elastic_password"
    # The node answers 401 before its security index is usable; a reset in that window fails (observed 2026-11-18 on
    # t2-dev: one attempt, exit non-zero, the old password file truncated). Retry for up to two minutes; the value goes
    # straight from the tool's stdout to the 0600 file on every attempt and is never printed.
    for _ in range(20):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            r = subprocess.run(["docker", "exec", NAME, "bin/elasticsearch-reset-password", "-u", "elastic", "-a", "-b", "-s"],
                               stdout=fd, stderr=subprocess.DEVNULL)
        finally:
            os.close(fd)
        if r.returncode == 0 and p.stat().st_size > 0:
            break
        time.sleep(6)
    else:
        raise SystemExit("password reset failed after 20 attempts (nothing printed)")
    from . import es_keys
    print("security on; key id fingerprints", es_keys.create())


def disable():
    for f in list(config.SECRETS.glob("es_api_key*.json")):
        f.unlink()
    _recreate(False)
    print("security off; key files removed")


if __name__ == "__main__":
    {"enable": enable, "disable": disable}[sys.argv[1]]()
