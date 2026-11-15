"""Shared plumbing for the evolution experiments. Uses its own database `evo` in the existing throwaway
Postgres container and tenants/indices prefixed sys-<tenant> with tenant names starting 'ev', 'f9', 'f10', 'f11'."""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # systems/
for p in (ROOT, os.path.join(ROOT, "exp")):
    if p not in sys.path:
        sys.path.insert(0, p)
import psycopg
from memsys import db as _db
from memsys.esc import ES

RES = os.environ.get("RESDIR", os.path.expanduser("~/emlab-16096/systems/results"))
os.makedirs(RES, exist_ok=True)


def connect(autocommit=True, application_name="evo"):
    port = int((_db.HOME / "pg_port").read_text().strip())
    os.environ.setdefault("PGPASSFILE", str(_db.HOME / "pgpass"))
    return psycopg.connect(host="127.0.0.1", port=port, dbname="evo", user="emlab", autocommit=autocommit, application_name=application_name)


def reset(conn):
    _db.reset_schema(conn)


def save(name, obj):
    with open(f"{RES}/{name}.json", "w") as f:
        json.dump(obj, f, indent=1, default=str)


def disk_free_gb():
    st = os.statvfs("/")
    return round(st.f_bavail * st.f_frsize / 1e9, 1)
