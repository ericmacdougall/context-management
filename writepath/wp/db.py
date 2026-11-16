"""Postgres connections. Passwords via a 0600 pgpass file (never argv or environment values), one entry per role.

Roles: wp_app (the application; default), wp_verifier (erasure completion), wp_kms (the key-service stand-in),
and the cluster admin (setup, migrations and experiment controls only)."""
import os

import psycopg

from . import config

APP, VERIFIER, KMS = "wp_app", "wp_verifier", "wp_kms"


def connect(db=None, autocommit=True, application_name="wp", role=APP, **kw):
    os.environ["PGPASSFILE"] = config.pgpass()
    return psycopg.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=db or config.EVENTS_DB, user=role,
                           autocommit=autocommit, application_name=application_name, **kw)


def admin(db=None, **kw):
    """Cluster admin: setup, migrations, experiment controls. Never used by the write path."""
    return connect(db=db or config.EVENTS_DB, role=config.PG_USER, application_name="wp-admin", **kw)


def keys_connect(**kw):
    return connect(db=config.KEYS_DB, application_name="wp-keys", role=KMS, **kw)


def verifier(db=None, **kw):
    return connect(db=db or config.EVENTS_DB, application_name="wp-verifier", role=VERIFIER, **kw)
