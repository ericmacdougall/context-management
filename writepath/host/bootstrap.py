"""Create this stream's own lab home and stores on the fleet host (idempotent). Standalone: system python3 + docker,
does not import wp (whose config refuses to start until this has written the conf).

  WP_HOME=$HOME/emlab2-16096/wphard/wp python3 host/bootstrap.py PG_PORT ES_PORT

- WP_HOME must end with emlab2-16096/wphard/wp (never the previous lab's ~/emlab-16096).
- Containers emlab2-wphard-pg (postgres:16) and emlab2-wphard-es (elasticsearch 9.5.6, security off; D6 turns it
  on with wp/es_security.py), loopback ports only, labelled emlab2.stream=wphard. A container of that name without
  the label is refused, never removed.
- STORAGE (the stamped-run gate's precondition, runkit/GATE-DESIGN.md 5.6, B3 and B4 on fix/writepath-gate at
  a293050): each store's data directory is a private tmpfs (--tmpfs, nr_inodes=0) whose size exceeds the container's
  memory limit (memory-swap equal to it: no swap) by the store's largest free-space threshold plus a margin, so the
  data path can never reach a watermark (ES) or fill (PG), and no other tenant of the host disk can write into it.
  Sizes (ASSUMPTION, the gate design's figures; this stream's largest live steps use well under 1 GiB of store data):
    PG  memory 4 GiB, tmpfs 4.5 GiB (S - M = 0.5 GiB >= the 0.25 GiB margin)
    ES  memory 6 GiB, tmpfs 16.5 GiB (S - M = 10.5 GiB >= the 10 GiB low watermark + 0.25 GiB margin)
  At most 10 GiB of host memory is held. A stop, restart or recreate of a container EMPTIES its store: bootstrap and
  wp.setup run again (both idempotent) and recreate what lived there (roles, databases, ES users and keys). So the PG
  settings that need a restart are passed on the server's command line, never by ALTER SYSTEM plus a restart.
- Elasticsearch disk watermarks are ABSOLUTE free-space floors (low 10gb, high 9gb, flood 8gb; amendment H6d, the
  user's lab disk rule, were 18/17/15gb): at 8 GB free the node stops writes itself, which enforces the stop line from
  inside the store. wp/es_security.py applies the same values when it recreates the node.
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

PG_C, ES_C = "emlab2-wphard-pg", "emlab2-wphard-es"
LABEL = "emlab2.stream=wphard"
PG_IMAGE, ES_IMAGE = "postgres:16", "docker.elastic.co/elasticsearch/elasticsearch:9.5.6"
GIB = 1024 ** 3
PG_DATA, ES_DATA = "/var/lib/postgresql/data", "/usr/share/elasticsearch/data"
PG_MEM, PG_TMPFS = 4 * GIB, 4 * GIB + GIB // 2
ES_MEM, ES_TMPFS = 6 * GIB, 16 * GIB + GIB // 2
WATERMARKS = {"low": "10gb", "high": "9gb", "flood_stage": "8gb"}          # amendment H6d (wp/es_security.py: the same)
ES_LOW_BYTES = 10 * GIB                       # the largest free-space threshold (ES byte units: gb = 1024^3 bytes)
MARGIN = GIB // 4                             # the gate's MARGIN (256 MiB)
# the earlier lab's node parity and amendment 17c (WAL cap), on the command line: a restart would empty the tmpfs
PG_SETTINGS = {"max_wal_size": "256MB", "min_wal_size": "64MB", "wal_recycle": "off", "wal_keep_size": "0",
               "archive_mode": "off", "max_connections": "200"}
assert PG_TMPFS - PG_MEM >= MARGIN and ES_TMPFS - ES_MEM >= ES_LOW_BYTES + MARGIN


def tmpfs_arg(path, size):
    return ["--tmpfs", f"{path}:rw,size={size},nr_inodes=0,mode=1777"]


def pg_run_args(pg_port, pw_file):
    return (["docker", "run", "-d", "--name", PG_C, "--label", LABEL, "--memory", str(PG_MEM), "--memory-swap", str(PG_MEM),
             "--shm-size", "256m", *tmpfs_arg(PG_DATA, PG_TMPFS), "-p", f"127.0.0.1:{pg_port}:5432",
             "-v", f"{pw_file}:/run/secrets/pgpw:ro", "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/pgpw",
             "-e", "POSTGRES_USER=wp", "-e", "POSTGRES_DB=wp_events", "-e", f"PGDATA={PG_DATA}", PG_IMAGE, "postgres"]
            + [x for k, v in PG_SETTINGS.items() for x in ("-c", f"{k}={v}")])


def es_run_args(es_port):
    return (["docker", "run", "-d", "--name", ES_C, "--label", LABEL, "--memory", str(ES_MEM), "--memory-swap", str(ES_MEM),
             *tmpfs_arg(ES_DATA, ES_TMPFS), "-p", f"127.0.0.1:{es_port}:9200",
             "-e", "ES_JAVA_OPTS=-Xms1g -Xmx1g", "-e", "discovery.type=single-node",
             "-e", "xpack.security.enabled=false", "-e", "action.auto_create_index=false"]
            + [x for k, v in WATERMARKS.items() for x in ("-e", f"cluster.routing.allocation.disk.watermark.{k}={v}")]
            + [ES_IMAGE])


def _home():
    h = os.environ.get("WP_HOME", "")
    if not h.rstrip("/").endswith("emlab2-16096/wphard/wp"):
        raise SystemExit("refusing: WP_HOME must be this stream's home (.../emlab2-16096/wphard/wp)")
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
    if (j["Config"].get("Labels") or {}).get("emlab2.stream") != "wphard":
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
    conf = {"stream": "emlab2-wphard", "pg_port": str(pg_port), "es_url": f"http://127.0.0.1:{es_port}",
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
        subprocess.run(pg_run_args(pg_port, sec / "pg_password"), check=True, capture_output=True)
    if _container(ES_C) is None:
        if not _port_free(es_port):
            raise SystemExit(f"refusing: port {es_port} is in use")
        subprocess.run(es_run_args(es_port), check=True, capture_output=True)

    t0 = time.time()
    while subprocess.run(["docker", "exec", PG_C, "pg_isready", "-U", "wp", "-q"]).returncode != 0:
        if time.time() - t0 > 120:
            raise SystemExit("postgres not ready in 120 s")
        time.sleep(2)
    while True:
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{es_port}/", timeout=5).status == 200:
                break
        except urllib.error.HTTPError as e:
            if e.code == 401:          # security on (wp/es_security.py): an anonymous request is refused once HTTP serves
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
    # amendment 17c (WAL cap) and the earlier lab's node parity (wal_recycle=off matters to the key-residue proofs: a
    # recycled WAL segment keeps old bytes on disk; max_connections to the concurrent runs) are on the server's
    # command line (PG_SETTINGS): checked here, never changed by ALTER SYSTEM and a restart (the tmpfs would empty)
    got = dict(ln.split("|", 1) for ln in _psql("select name || '|' || current_setting(name) from pg_settings where name in ("
                                                 + ", ".join(f"'{k}'" for k in PG_SETTINGS) + ");", "postgres").split())
    if got != PG_SETTINGS:
        raise SystemExit("postgres settings differ from the command line: "
                         f"{sorted(set(got.items()) ^ set(PG_SETTINGS.items()))}")
    print("bootstrap ok", json.dumps({k: v for k, v in conf.items()}), "secrets: files only, nothing printed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(int(sys.argv[1]), int(sys.argv[2]))
