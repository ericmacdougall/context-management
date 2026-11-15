"""Paths and connections. Everything lives under ENGINE_HOME (default ~/emlab-16096/engine on t2-dev). Postgres host
and port come from pg/pg_port; the password is read by libpq from the 0600 pg/pgpass file (never in env or argv).

There is NO tenant constant for the engine: activities and pgstore take the tenant explicitly. DEMO_TENANT is only
the tenant the single-tenant experiment drivers choose when they play the API side."""
import os
import pathlib

HOME = pathlib.Path(os.environ.get("ENGINE_HOME", os.path.expanduser("~/emlab-16096/engine")))
DEMO_TENANT = "t1"


def pg_connect(autocommit=False, app="engine"):
    import psycopg
    os.environ["PGPASSFILE"] = str(HOME / "pg" / "pgpass")
    port = int((HOME / "pg" / "pg_port").read_text().strip())
    return psycopg.connect(host="127.0.0.1", port=port, dbname="emlab", user="emlab",
                           autocommit=autocommit, application_name=app)


def temporal_target():
    if os.environ.get("ENGINE_TEMPORAL_PORT"):
        return "127.0.0.1:" + os.environ["ENGINE_TEMPORAL_PORT"]
    return "127.0.0.1:" + (HOME / "run" / "temporal_port").read_text().strip()
