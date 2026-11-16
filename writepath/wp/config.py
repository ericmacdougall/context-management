"""Deployment settings, read from files under WP_HOME (default ~/emlab-16096/wp).

Nothing secret is passed on a command line or in the environment: the Postgres password sits in a 0600 pgpass file,
and an Elasticsearch API key (if the deployment uses one) in a 0600 file. Values are never printed or logged."""
import json
import os
import pathlib

HOME = pathlib.Path(os.environ.get("WP_HOME", os.path.expanduser("~/emlab-16096/wp")))
SECRETS = HOME / "secrets"


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


def es_auth_header():
    """Optional API key for a secured cluster (file secrets/es_api_key.json with an 'encoded' field)."""
    p = SECRETS / "es_api_key.json"
    if p.exists():
        return "ApiKey " + json.loads(p.read_text())["encoded"]
    return None


def pgpass(role=None):
    """The cluster admin's file (role None or the admin user) or the role's own file."""
    if role is None or role == PG_USER:
        return str(SECRETS / "pgpass")
    return str(SECRETS / f"pgpass.{role}")
