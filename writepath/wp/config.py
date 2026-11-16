"""Deployment settings, read from files under WP_HOME (default ~/emlab-16096/wp).

Nothing secret is passed on a command line or in the environment: the Postgres password sits in a 0600 pgpass file,
and an Elasticsearch API key (if the deployment uses one) in a 0600 file. Values are never printed or logged."""
import json
import os
import pathlib

HOME = pathlib.Path(os.environ.get("WP_HOME", os.path.expanduser("~/emlab-16096/wp")))
# D2: a service process may be given its OWN credential directory (a path, not a secret), holding only its own files;
# the lab uses this to run the verifier as a separate process that the application process cannot impersonate.
SECRETS = pathlib.Path(os.environ["WP_SECRETS_DIR"]) if os.environ.get("WP_SECRETS_DIR") else HOME / "secrets"


def _read(name, default=None):
    p = HOME / "conf" / name
    return p.read_text().strip() if p.exists() else default


PG_HOST = "127.0.0.1"
PG_PORT = int(_read("pg_port", "25460"))
PG_USER = _read("pg_user", "wp")
EVENTS_DB = _read("events_db", "wp_events")
KEYS_DB = _read("keys_db", "wp_keys")
ES_URL = _read("es_url", "http://127.0.0.1:19260")
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
