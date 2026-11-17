"""Per-role Elasticsearch API keys (delta review D6).

Without security every holder of the one credential can write by concrete index name, write a non-placeholder through
the erase alias `-x`, or delete indices; require_alias is only a request flag. With security on, each writer role gets
its own API key whose privileges make the fence structural:

  projector  creates the family index and manages its aliases (manage on the base index and on the read, write and
             erase alias names: the alias CAS needs it); WRITES documents only through the epoch write aliases
             (-w<E>); no write privilege on the base index or on -x. RESIDUAL: `manage` lets it delete its family
             indices; that loses projected data (rebuilt from Postgres) but cannot plant content.
  eraser     writes only through the erase alias (-x): placeholders; view_index_metadata to find the family.
  verifier   read, monitor and maintenance (refresh, flush, force-merge) on the stream's indices; monitor and
             view_index_metadata on every index (the unregistered-store gate); no write anywhere.
  admin      the lab's administrative key (setup, experiments, controls): never used by the write path.

Keys are created by the cluster superuser `elastic`, whose password is read from a 0600 file and never printed. Each
key is written to its own 0600 file secrets/es_api_key.<role>.json ({"id", "encoded"}); nothing secret is printed.
The key broker (wp/broker.py, `python -m wp.es_keys broker`) is a native user with its own role; in broker mode the
write path uses only keys the broker mints per tenant and epoch, and the role-key files above are not used by it.
usage: python -m wp.es_keys create|broker|invalidate   (requires security on; see wp/es_security.py)"""
import base64
import json
import os
import sys

from . import config
from .es import ES

P = config.INDEX_PREFIX
_T = "[a-z0-9]+"
_F = "(events|facts)"
BASE = f"/{P}{_T}-{_F}-base/"
READ = f"/{P}{_T}-{_F}/"
WRITE = f"/{P}{_T}-{_F}-w[0-9]+/"
ERASE = f"/{P}{_T}-{_F}-x/"

ROLE_DESCRIPTORS = {
    "projector": {"cluster": ["monitor"], "indices": [
        {"names": [BASE], "privileges": ["create_index", "view_index_metadata", "manage"]},
        {"names": [READ, ERASE], "privileges": ["view_index_metadata", "manage"]},
        {"names": [WRITE], "privileges": ["view_index_metadata", "manage", "create_doc", "index"]}]},
    "eraser": {"cluster": ["monitor"], "indices": [
        {"names": [BASE, READ, WRITE], "privileges": ["view_index_metadata"]},
        {"names": [ERASE], "privileges": ["view_index_metadata", "index"]}]},
    "verifier": {"cluster": ["monitor"], "indices": [
        {"names": [f"{P}*"], "privileges": ["read", "monitor", "maintenance", "view_index_metadata"]},
        {"names": ["*"], "privileges": ["monitor", "view_index_metadata"], "allow_restricted_indices": True}]},
}


def _superuser_header():
    pw = (config.SECRETS / "es_elastic_password").read_text().strip()
    h = "Basic " + base64.b64encode(f"elastic:{pw}".encode()).decode()
    del pw
    return h


def _write(name, obj):
    p = config.SECRETS / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(obj).encode())
    finally:
        os.close(fd)
    os.chmod(p, 0o600)


def create():
    es = ES()
    es.auth = _superuser_header()
    out = {}
    for role, rd in ROLE_DESCRIPTORS.items():
        j = es.req("POST", "/_security/api_key", {"name": f"wp16096-{role}", "role_descriptors": {f"wp16096_{role}": rd},
                                                 "metadata": {"stream": "goodqa-16096", "role": role}})[1]
        _write(f"es_api_key.{role}.json", {"id": j["id"], "encoded": j["encoded"]})
        out[role] = j["id"]
    j = es.req("POST", "/_security/api_key", {"name": "wp16096-admin", "metadata": {"stream": "goodqa-16096", "role": "admin"}})[1]
    _write("es_api_key.json", {"id": j["id"], "encoded": j["encoded"]})
    out["admin"] = j["id"]
    return out                      # key ids only (not secret)


def create_broker():
    """The key broker's own identity (wp/broker.py): role wp16096_broker_role and native user wp16096_broker, password
    generated here into secrets/es_broker_password (0600, never printed). Idempotent: rerunning rotates the password."""
    import secrets as _s
    from . import broker as B
    es = ES(auth=_superuser_header())
    es.req("PUT", f"/_security/role/{B.BROKER_ROLE}", B.broker_role_descriptor())
    pw = _s.token_urlsafe(32)
    p = config.SECRETS / B.PWFILE_NAME
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (pw + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(p, 0o600)
    es.req("PUT", f"/_security/user/{B.BROKER_USER}", {"password": pw, "roles": [B.BROKER_ROLE],
                                                      "metadata": {"stream": "goodqa-16096", "role": "broker"}})
    del pw
    return B.BROKER_USER


def invalidate_all():
    es = ES()
    es.auth = _superuser_header()
    return es.req("DELETE", "/_security/api_key", {"name": "wp16096-*"}, ok=(200, 404))[1].get("invalidated_api_keys", [])


if __name__ == "__main__":
    if sys.argv[1:] == ["create"]:
        print("created key ids", create())
    elif sys.argv[1:] == ["broker"]:
        print("broker user", create_broker())
    elif sys.argv[1:] == ["invalidate"]:
        print("invalidated", len(invalidate_all()))
    else:
        raise SystemExit(__doc__)
