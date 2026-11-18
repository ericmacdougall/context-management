"""Postgres connections. Passwords via a 0600 pgpass file (never argv or environment values), one entry per role.

Roles: wp_app (the application; default), wp_verifier (erasure completion), wp_kms (the key-service stand-in),
wp_legal (legal holds), wp_broker (the D6 key broker: lease check only), wp_sealer (the seal authority: SELECT on
identifier_epochs and subject_identifiers), and the cluster admin (setup, migrations and experiment controls only).
D2: every role has its own credential file. In this lab all files belong to one OS user, so the separation is per file,
not per OS identity; a deployment runs each role as its own service identity that can read only its own secret."""
import os

import psycopg

from . import config

APP, VERIFIER, KMS, LEGAL, WATCHDOG, BROKER = "wp_app", "wp_verifier", "wp_kms", "wp_legal", "wp_watchdog", "wp_broker"
SEALER = "wp_sealer"                                # the seal authority (wp/sealer.py): reads identifiers only
REVIEWER_GROUP = "wp_reviewer"                     # D3: NOLOGIN group; approves identifier proposals
REVIEWERS = ("wp_rev1", "wp_rev2")                 # one login per reviewer principal (members of REVIEWER_GROUP)


def connect(db=None, autocommit=True, application_name="wp", role=APP, **kw):
    """Each role authenticates from ITS OWN 0600 file (config.pgpass(role)); see D2 in the module docstring."""
    return psycopg.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=db or config.EVENTS_DB, user=role,
                           autocommit=autocommit, application_name=application_name, passfile=config.pgpass(role), **kw)


def admin(db=None, **kw):
    """Cluster admin: setup, migrations, experiment controls. Never used by the write path."""
    return connect(db=db or config.EVENTS_DB, role=config.PG_USER, application_name="wp-admin", **kw)


def keys_connect(**kw):
    return connect(db=config.KEYS_DB, application_name="wp-keys", role=KMS, **kw)


def legal(**kw):
    """Legal-operations role: places and releases holds; nothing else."""
    return connect(application_name="wp-legal", role=LEGAL, **kw)


def watchdog(**kw):
    """Transaction-age watchdog (wp/xact_watchdog.py): pg_signal_backend + pg_read_all_stats, no table access."""
    return connect(db="postgres", application_name="wp-watchdog", role=WATCHDOG, **kw)


def verifier(db=None, **kw):
    return connect(db=db or config.EVENTS_DB, application_name="wp-verifier", role=VERIFIER, **kw)


def broker(**kw):
    """D6 key broker: executes wp_lease_holder_current only."""
    return connect(application_name="wp-broker", role=BROKER, **kw)


def reviewer(n=1, **kw):
    """D3 reviewer principal n (wp_rev<n>): approves or rejects identifier proposals; may propose, never approve its own."""
    return connect(application_name=f"wp-reviewer{n}", role=REVIEWERS[n - 1], **kw)
