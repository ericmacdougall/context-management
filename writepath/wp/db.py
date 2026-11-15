"""Postgres connections. Password via a 0600 pgpass file (never argv or environment values)."""
import os

import psycopg

from . import config


def connect(db=None, autocommit=True, application_name="wp", **kw):
    os.environ["PGPASSFILE"] = config.pgpass()
    c = psycopg.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=db or config.EVENTS_DB, user=config.PG_USER,
                        autocommit=autocommit, application_name=application_name, **kw)
    return c


def keys_connect(**kw):
    return connect(db=config.KEYS_DB, application_name="wp-keys", **kw)
