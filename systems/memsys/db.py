"""Postgres access. Host, port and password come from files under MEMSYS_HOME
(default ~/emlab-16096/systems): `pg_port` and a 0600 `pgpass`. Nothing secret is
passed on a command line or in the environment."""
import os
import pathlib
from datetime import timezone

import psycopg

HOME = pathlib.Path(os.environ.get("MEMSYS_HOME", os.path.expanduser("~/emlab-16096/systems")))
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def connect(autocommit=False, application_name="memsys"):
    port = int((HOME / "pg_port").read_text().strip())
    os.environ.setdefault("PGPASSFILE", str(HOME / "pgpass"))
    return psycopg.connect(host="127.0.0.1", port=port, dbname="emlab", user="emlab",
                           autocommit=autocommit, application_name=application_name)


def reset_schema(conn):
    conn.autocommit = True
    conn.execute("drop schema public cascade; create schema public;")
    conn.execute(SCHEMA.read_text())


def iso(dt) -> str:
    """Canonical fixed-width UTC timestamp used in hashes and order keys."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def xmin(cur) -> int:
    """Oldest still-running xid: every row with commit_xid below it is final."""
    return int(cur.execute("select pg_snapshot_xmin(pg_current_snapshot())::text").fetchone()[0])
