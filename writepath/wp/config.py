"""Deployment settings, read from files under WP_HOME/conf. WP_HOME is REQUIRED (no default).

Fail closed: the process refuses to start unless WP_HOME is set, WP_HOME/conf/stream names this stream
(STREAM below), and every store address and container name comes from that conf directory. There are no built-in
ports or container names, so a process that loses its environment (a child, a cron line, a copied tree) can never
reach another stream's stores. Container names must start with OWN_PREFIX.

Nothing secret is passed on a command line or in the environment: the Postgres password sits in a 0600 pgpass file,
and an Elasticsearch API key (if the deployment uses one) in a 0600 file. Values are never printed or logged."""
import json
import os
import pathlib
import urllib.parse

STREAM = "emlab2-writepath"        # the conf marker this code accepts
OWN_PREFIX = "emlab2-writepath-"   # every container this code may address by name starts with this


class NotThisStream(SystemExit):
    pass


if not os.environ.get("WP_HOME"):
    raise NotThisStream("refusing to start: WP_HOME is not set (there is no default home)")
HOME = pathlib.Path(os.environ["WP_HOME"])
# D2: a service process may be given its OWN credential directory (a path, not a secret), holding only its own files;
# the lab uses this to run the verifier as a separate process that the application process cannot impersonate.
SECRETS = pathlib.Path(os.environ["WP_SECRETS_DIR"]) if os.environ.get("WP_SECRETS_DIR") else HOME / "secrets"


def _read(name, default=None):
    p = HOME / "conf" / name
    return p.read_text().strip() if p.exists() else default


def _required(name):
    v = _read(name)
    if not v:
        raise NotThisStream(f"refusing to start: {HOME}/conf/{name} is missing")
    return v


if _read("stream") != STREAM:
    raise NotThisStream(f"refusing to start: {HOME}/conf/stream does not name {STREAM}")

PG_HOST = "127.0.0.1"
PG_PORT = int(_required("pg_port"))
PG_USER = _read("pg_user", "wp")
EVENTS_DB = _read("events_db", "wp_events")
KEYS_DB = _read("keys_db", "wp_keys")
ES_URL = _required("es_url")
ES_PORT = urllib.parse.urlsplit(ES_URL).port
PG_CONTAINER = _required("pg_container")
ES_CONTAINER = _required("es_container")
for _c in (PG_CONTAINER, ES_CONTAINER):
    if not _c.startswith(OWN_PREFIX):
        raise NotThisStream(f"refusing to start: container {_c!r} does not start with {OWN_PREFIX}")
if urllib.parse.urlsplit(ES_URL).hostname != "127.0.0.1":
    raise NotThisStream("refusing to start: es_url must be loopback")
INDEX_PREFIX = "wp16096-"          # every index, alias and template this system creates starts with this
LOG_DIR = HOME / "logs"


ES_ROLES = ("projector", "eraser", "verifier")       # D6: one API key per writer role (wp/es_keys.py)


def es_auth_header(role=None):
    """API key for a secured cluster: secrets/es_api_key.<role>.json for a role (D6), else secrets/es_api_key.json
    (the lab's administrative key, used by setup and experiments). None when the cluster has no security. A role
    whose key file is missing falls back to the admin file only if no role keys exist at all (security off)."""
    if role is not None:
        assert role in ES_ROLES, role
        p = SECRETS / f"es_api_key.{role}.json"
        if p.exists():
            return "ApiKey " + json.loads(p.read_text())["encoded"]
        if any((SECRETS / f"es_api_key.{r}.json").exists() for r in ES_ROLES):
            raise FileNotFoundError(f"role keys are configured but this process has none for {role}")
    p = SECRETS / "es_api_key.json"
    if p.exists():
        return "ApiKey " + json.loads(p.read_text())["encoded"]
    return None


def pgpass(role=None):
    """The cluster admin's file (role None or the admin user) or the role's own file."""
    if role is None or role == PG_USER:
        return str(SECRETS / "pgpass")
    return str(SECRETS / f"pgpass.{role}")
