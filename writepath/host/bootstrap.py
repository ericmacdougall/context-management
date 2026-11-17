"""Create this stream's own lab home and stores on the fleet host (idempotent). Standalone: system python3 + docker,
does not import wp (whose config refuses to start until this has written the conf).

  WP_HOME=$HOME/emlab2-16096/writepath/wp python3 host/bootstrap.py PG_PORT ES_PORT

- WP_HOME must end with emlab2-16096/writepath/wp (never the previous lab's ~/emlab-16096).
- Containers emlab2-writepath-pg (postgres:16) and emlab2-writepath-es (elasticsearch 9.5.6, security off; D6 turns it
  on with wp/es_security.py), loopback ports only, memory limits, labelled emlab2.stream=writepath. A container of
  that name without the label is refused, never removed.
- Elasticsearch disk watermarks are ABSOLUTE free-space floors (low 18gb, high 17gb, flood 15gb): at 15 GB free the
  node stops writes itself, which enforces the host disk rule (do not leave under 15 GB free) from inside the store.
- Secrets: the admin Postgres password and the two legacy-code role passwords are generated here into 0600 files
  under WP_HOME/secrets (0700) and WP_HOME/oldhome. Role passwords reach Postgres only as SCRAM verifiers computed
  here, so no cleartext reaches a statement, the server log, argv or the environment. Nothing secret is printed.
- Legacy home for old_repro (the a13b524 code): roles emlab and kms, databases emlab and keys; the code itself is
  shipped from git (git archive a13b524 systems), not copied from another tree."""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

PG_C, ES_C = "emlab2-writepath-pg", "emlab2-writepath-es"
LABEL = "emlab2.stream=writepath"
PG_IMAGE, ES_IMAGE = "postgres:16", "docker.elastic.co/elasticsearch/elasticsearch:9.5.6"


def _home():
    h = os.environ.get("WP_HOME", "")
    if not h.rstrip("/").endswith("emlab2-16096/writepath/wp"):
        raise SystemExit("refusing: WP_HOME must be this stream's home (.../emlab2-16096/writepath/wp)")
    return pathlib.Path(h)


def _write_secret(p, data):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)
    os.chmod(p, 0o600)


def _scram(pw, iterations=4096):
    """PostgreSQL SCRAM-SHA-256 verifier (RFC 5802/7677), as libpq's PQencryptPasswordConn computes it."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored = hashlib.sha256(client_key).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b(salt)}${b(stored)}:{b(server)}"


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _container(name):
    r = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    j = json.loads(r.stdout)[0]
    if (j["Config"].get("Labels") or {}).get("emlab2.stream") != "writepath":
        raise SystemExit(f"refusing: container {name} exists and is not this stream's (no {LABEL} label)")
    return j


def _psql(sql, db="postgres"):
    """SQL on stdin through the container's local socket (trust for local connections in the image). Nothing secret
    is in this SQL except SCRAM verifiers."""
    r = subprocess.run(["docker", "exec", "-i", PG_C, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-tA", "-U", "wp", "-d", db],
                       input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("psql failed: " + r.stderr.strip()[:300])
    return r.stdout


def main(pg_port, es_port):
    home = _home()
    for d in ("conf", "results", "logs", "anchors", "runs", "oldsrc", "oldhome"):
        (home / d).mkdir(parents=True, exist_ok=True)
    sec = home / "secrets"
    sec.mkdir(mode=0o700, exist_ok=True)
    os.chmod(sec, 0o700)
    os.chmod(home / "oldhome", 0o700)
    conf = {"stream": "emlab2-writepath", "pg_port": str(pg_port), "es_url": f"http://127.0.0.1:{es_port}",
            "pg_container": PG_C, "es_container": ES_C}
    for k, v in conf.items():
        (home / "conf" / k).write_text(v + "\n")

    if not (sec / "pg_password").exists():
        _write_secret(sec / "pg_password", secrets.token_urlsafe(32))
    pw = (sec / "pg_password").read_text().strip()
    _write_secret(sec / "pgpass", f"127.0.0.1:{pg_port}:*:wp:{pw}\n")
    del pw

    if _container(PG_C) is None:
        if not _port_free(pg_port):
            raise SystemExit(f"refusing: port {pg_port} is in use")
        subprocess.run(["docker", "run", "-d", "--name", PG_C, "--label", LABEL, "-m", "2g", "--shm-size", "256m",
                        "-p", f"127.0.0.1:{pg_port}:5432", "-v", f"{PG_C}-data:/var/lib/postgresql/data",
                        "-v", f"{sec / 'pg_password'}:/run/secrets/pgpw:ro", "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/pgpw",
                        "-e", "POSTGRES_USER=wp", "-e", "POSTGRES_DB=wp_events", PG_IMAGE], check=True, capture_output=True)
    if _container(ES_C) is None:
        if not _port_free(es_port):
            raise SystemExit(f"refusing: port {es_port} is in use")
        subprocess.run(["docker", "run", "-d", "--name", ES_C, "--label", LABEL, "-m", "2g",
                        "-p", f"127.0.0.1:{es_port}:9200", "-v", f"{ES_C}-data:/usr/share/elasticsearch/data",
                        "-e", "ES_JAVA_OPTS=-Xms1g -Xmx1g", "-e", "discovery.type=single-node",
                        "-e", "xpack.security.enabled=false", "-e", "action.auto_create_index=false",
                        "-e", "cluster.routing.allocation.disk.watermark.low=18gb",
                        "-e", "cluster.routing.allocation.disk.watermark.high=17gb",
                        "-e", "cluster.routing.allocation.disk.watermark.flood_stage=15gb",
                        ES_IMAGE], check=True, capture_output=True)

    t0 = time.time()
    while subprocess.run(["docker", "exec", PG_C, "pg_isready", "-U", "wp", "-q"]).returncode != 0:
        if time.time() - t0 > 120:
            raise SystemExit("postgres not ready in 120 s")
        time.sleep(2)
    while True:
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{es_port}/", timeout=5).status == 200:
                break
        except (urllib.error.URLError, OSError):
            pass
        if time.time() - t0 > 300:
            raise SystemExit("elasticsearch not ready in 300 s")
        time.sleep(3)

    # legacy home for the a13b524 code (old_repro): roles emlab and kms, databases emlab and keys
    oh = home / "oldhome"
    (oh / "pg_port").write_text(f"{pg_port}\n")
    (oh / "kms_port").write_text(f"{pg_port}\n")
    lines = []
    for role in ("emlab", "kms"):
        pf = sec / f"pw_{role}"
        if not pf.exists():
            _write_secret(pf, secrets.token_urlsafe(32))
        rpw = pf.read_text().strip()
        lines.append(f"127.0.0.1:{pg_port}:*:{role}:{rpw}")
        v = _scram(rpw)
        del rpw
        _psql(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') then "
              f"create role {role} login nosuperuser nocreatedb nocreaterole; end if; end $$;\n"
              f"alter role {role} password '{v}';\n")
    _write_secret(oh / "pgpass", "\n".join(lines) + "\n")
    have = set(_psql("select datname from pg_database;", "postgres").split())
    for dbn, owner in (("emlab", "emlab"), ("keys", "kms")):
        if dbn not in have:
            _psql(f"create database {dbn} owner {owner};")
        _psql(f"alter schema public owner to {owner};", dbn)
    # the old key service destroys a key with VACUUM, pg_switch_wal() and CHECKPOINT (a13b524 memsys/kms.py purge);
    # without these the old erasure raises and old_repro would "reproduce" a finding for an environment reason
    _psql("grant pg_checkpoint to kms;", "postgres")
    _psql("grant execute on function pg_switch_wal() to kms;", "keys")
    # amendment 17c: cap the WAL this node keeps (the host's free space swings by several GB; the run's own growth is
    # capped at 2 GiB). A deviation from the earlier lab's default max_wal_size of 1GB, stated in THRESHOLDS.
    _psql("alter system set max_wal_size = '256MB';\nalter system set min_wal_size = '64MB';\nselect pg_reload_conf();", "postgres")
    # parity with the earlier lab's node (its container ran postgres -c wal_recycle=off -c wal_keep_size=0
    # -c archive_mode=off -c max_connections=200, recorded nowhere in the repo until now): wal_recycle=off matters to
    # the key-residue proofs (a recycled WAL segment keeps old bytes on disk), max_connections to the concurrent runs
    _psql("alter system set wal_recycle = off;\nalter system set wal_keep_size = 0;\nalter system set archive_mode = off;\n"
          "alter system set max_connections = 200;\nselect pg_reload_conf();", "postgres")
    if _psql("select count(*) from pg_settings where pending_restart;", "postgres").strip() != "0":
        subprocess.run(["docker", "restart", PG_C], check=True, capture_output=True)
        t1 = time.time()
        while subprocess.run(["docker", "exec", PG_C, "pg_isready", "-U", "wp", "-q"]).returncode != 0:
            if time.time() - t1 > 120:
                raise SystemExit("postgres not ready in 120 s after the settings restart")
            time.sleep(2)
    print("bootstrap ok", json.dumps({k: v for k, v in conf.items()}), "secrets: files only, nothing printed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(int(sys.argv[1]), int(sys.argv[2]))
