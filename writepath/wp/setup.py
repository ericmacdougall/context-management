"""Create (or reset, lab only) roles, databases and schemas. usage: python -m wp.setup [--reset]

Runs as the cluster admin. DDL runs under SET ROLE wp_owner (events) or SET ROLE wp_kms (keys), so the application
role owns nothing. Role passwords are generated here, hashed client-side (SCRAM, libpq) so no cleartext reaches a
statement or the server log, and stored only in the 0600 pgpass file. Nothing secret is printed."""
import os
import pathlib
import secrets
import sys

from psycopg import sql

from . import config, db, registry

HERE = pathlib.Path(__file__).parent
LOGIN_ROLES = (db.APP, db.VERIFIER, db.KMS, db.LEGAL, db.WATCHDOG, db.BROKER)
ROLE_TIMEOUTS = {"statement_timeout": "30s", "idle_in_transaction_session_timeout": "5s"}
ROLE_ATTRS = "nosuperuser nocreatedb nocreaterole noreplication nobypassrls inherit"


def _pgpass_roles():
    """Roles that already have their own credential file (D2: one 0600 file per role)."""
    return {r for r in LOGIN_ROLES if pathlib.Path(config.pgpass(r)).exists()}


def _ensure_roles(admin):
    have = {r[0] for r in admin.execute("select rolname from pg_roles").fetchall()}
    if "wp_owner" not in have:
        admin.execute("create role wp_owner nologin " + ROLE_ATTRS)
    known = _pgpass_roles()
    for r in LOGIN_ROLES:
        if r not in have:
            admin.execute(sql.SQL("create role {} login " + ROLE_ATTRS).format(sql.Identifier(r)))
        admin.execute(sql.SQL("alter role {} " + ROLE_ATTRS).format(sql.Identifier(r)))
        if r not in known:
            pw = secrets.token_urlsafe(32)
            h = admin.pgconn.encrypt_password(pw.encode(), r.encode(), b"scram-sha-256").decode()
            admin.execute(sql.SQL("alter role {} password {}").format(sql.Identifier(r), sql.Literal(h)))
            p = pathlib.Path(config.pgpass(r))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"{config.PG_HOST}:{config.PG_PORT}:*:{r}:{pw}\n".encode())
            finally:
                os.close(fd)
            os.chmod(p, 0o600)
            del pw
    # D2: the admin file keeps only the admin's own line (role secrets live in their own files, written above)
    ap = pathlib.Path(config.pgpass())
    lines = ap.read_text().splitlines()
    keep = [ln for ln in lines if ln.count(":") < 4 or ln.split(":")[3] not in LOGIN_ROLES]
    if len(keep) != len(lines):
        fd = os.open(ap, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, ("\n".join(keep) + "\n").encode())
        finally:
            os.close(fd)
    # liveness mitigations on the application role (F6): an idle-in-transaction session is ended, never left holding
    # a lease row lock or the cluster xmin.
    # Every login role: a transaction holds the cluster's oldest running xid only for a bounded time, because the
    # projector of EVERY tenant reads only below that xid (row 23 fence; exp/xmin_stall.py). Erasure, projection and
    # append statements are bounded batches well under statement_timeout.
    for r in LOGIN_ROLES:
        for k, v in ROLE_TIMEOUTS.items():
            admin.execute(sql.SQL("alter role {} set {} = {}").format(sql.Identifier(r), sql.Identifier(k), sql.Literal(v)))
    # D5: lock timeouts for every role (a waiting statement fails and is retried, never queues behind a long lock)
    for r in LOGIN_ROLES:
        admin.execute(sql.SQL("alter role {} set lock_timeout = '10s'").format(sql.Identifier(r)))


def main(reset):
    assert config.PG_CONTAINER.startswith(config.OWN_PREFIX)   # config refuses any other stream's conf
    admin = db.admin(db="postgres")
    _ensure_roles(admin)
    have = {r[0] for r in admin.execute("select datname from pg_database").fetchall()}
    for d in (config.EVENTS_DB, config.KEYS_DB):
        if d not in have:
            admin.execute(f'create database "{d}"')
    if "wp_ctl" in have:                     # this stream's old all-triggers-dropped control database: retired
        admin.execute('drop database "wp_ctl"')
    admin.execute(f'revoke connect on database "{config.EVENTS_DB}" from public')
    admin.execute(f'grant connect on database "{config.EVENTS_DB}" to wp_app, wp_verifier, wp_legal, wp_broker')
    admin.execute(f'revoke connect on database "{config.KEYS_DB}" from public')
    admin.execute(f'grant connect on database "{config.KEYS_DB}" to wp_kms, wp_verifier')
    admin.execute("grant pg_checkpoint to wp_kms with inherit true")
    admin.execute("grant pg_read_all_stats to wp_kms with inherit true")   # purge() reads other backends' xid/xmin
    # item 1 (4960f50 round): on PG 16 no setting bounds a transaction that keeps issuing short statements
    # (transaction_timeout arrives in PG 17); the watchdog terminates such backends (wp/xact_watchdog.py)
    admin.execute("grant pg_signal_backend, pg_read_all_stats to wp_watchdog with inherit true")
    ev, ks = db.admin(), db.admin(db=config.KEYS_DB)
    ks.execute("grant execute on function pg_switch_wal() to wp_kms")
    if reset:
        ev.execute("drop schema public cascade"); ev.execute("create schema public authorization wp_owner")
        ev.execute("set role wp_owner"); ev.execute((HERE / "schema.sql").read_text()); ev.execute("reset role")
        ks.execute("drop schema public cascade"); ks.execute("create schema public authorization wp_kms")
        ks.execute("set role wp_kms"); ks.execute((HERE / "keys_schema.sql").read_text()); ks.execute("reset role")
    ev.execute("set role wp_owner"); registry.sync_db(ev); ev.execute("reset role")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("setup ok", "reset" if reset else "")


if __name__ == "__main__":
    main("--reset" in sys.argv)
