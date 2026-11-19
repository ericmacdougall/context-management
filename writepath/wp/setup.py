"""Create (or reset, lab only) roles, databases and schemas. usage: python -m wp.setup [--reset]

Runs as the cluster admin. DDL runs under SET ROLE wp_owner (events) or SET ROLE wp_kms (keys), so the application
role owns nothing. Role passwords are generated here, hashed client-side (SCRAM, libpq) so no cleartext reaches a
statement or the server log, and stored only in the 0600 pgpass file. Nothing secret is printed."""
import contextlib
import os
import pathlib
import secrets
import sys

from psycopg import sql

from . import config, db, registry

HERE = pathlib.Path(__file__).parent
LOGIN_ROLES = (db.APP, db.VERIFIER, db.KMS, db.LEGAL, db.WATCHDOG, db.BROKER, db.SEALER) + db.REVIEWERS
ROLE_TIMEOUTS = {"statement_timeout": "30s", "idle_in_transaction_session_timeout": "5s"}
ROLE_ATTRS = "nosuperuser nocreatedb nocreaterole noreplication nobypassrls inherit"


# review 42cfd11 R7, R9: settings under which the server logs no statement text and no bind parameter, set LOCAL (one
# transaction; these are superuser settings, so they are set before any SET LOCAL ROLE)
QUIET = (("log_statement", "none"), ("log_min_duration_statement", "-1"), ("log_min_duration_sample", "-1"),
         ("log_min_error_statement", "panic"), ("log_parameter_max_length", "0"),
         ("log_parameter_max_length_on_error", "0"), ("log_error_verbosity", "terse"))


@contextlib.contextmanager
def quiet(conn):
    """One transaction on the admin connection in which statements and their parameters are not logged (a SCRAM
    verifier, which ALTER ROLE can only take as a literal; the seal pads). log_transaction_sample_rate is decided when a
    transaction starts (PostgreSQL's StartTransaction), so SET LOCAL cannot undo it: it is set to 0 at session level
    BEFORE the transaction and restored after, which needs an autocommit connection. Extensions that log on their own
    (auto_explain, pgaudit) are not covered: a deployment must not load them for the installing session."""
    if getattr(conn, "autocommit", True) is not True:
        raise RuntimeError("setup.quiet needs an autocommit connection (transaction sampling is decided at its start)")
    row = conn.execute("select current_setting('log_transaction_sample_rate')").fetchone()
    conn.execute("set log_transaction_sample_rate = 0")
    try:
        with conn.transaction():
            for g, v in QUIET:
                conn.execute(sql.SQL("set local {} = {}").format(sql.Identifier(g), sql.Literal(v)))
            yield conn
    finally:
        if row:
            conn.execute(sql.SQL("set log_transaction_sample_rate = {}").format(sql.Literal(row[0])))
        else:
            conn.execute("reset log_transaction_sample_rate")


def _pgpass_roles():
    """Roles that already have their own credential file (D2: one 0600 file per role)."""
    return {r for r in LOGIN_ROLES if pathlib.Path(config.pgpass(r)).exists()}


def _ensure_roles(admin):
    have = {r[0] for r in admin.execute("select rolname from pg_roles").fetchall()}
    if "wp_owner" not in have:
        admin.execute("create role wp_owner nologin " + ROLE_ATTRS)
    if db.REVIEWER_GROUP not in have:                # D3: reviewers are members of a NOLOGIN group
        admin.execute(sql.SQL("create role {} nologin " + ROLE_ATTRS).format(sql.Identifier(db.REVIEWER_GROUP)))
    known = _pgpass_roles()
    for r in LOGIN_ROLES:
        if r not in have:
            admin.execute(sql.SQL("create role {} login " + ROLE_ATTRS).format(sql.Identifier(r)))
        admin.execute(sql.SQL("alter role {} " + ROLE_ATTRS).format(sql.Identifier(r)))
        # a role without a password on the cluster gets one, whatever an existing pgpass file holds: the stores are on
        # tmpfs (host/bootstrap.py), so a recreated cluster keeps the files of the one before it (observed live
        # 2026-11-18 after the tmpfs rebuild: wp_app's login failed, and a setup that keyed on "created now" left the
        # roles of that first run without passwords). The admin reads pg_authid (superuser); no value is read back.
        no_pw = admin.execute("select rolpassword is null from pg_authid where rolname = %s", (r,)).fetchall()
        if r not in known or not no_pw or no_pw[0][0]:
            pw = secrets.token_urlsafe(32)
            h = admin.pgconn.encrypt_password(pw.encode(), r.encode(), b"scram-sha-256").decode()
            # review 42cfd11 R9: ALTER ROLE takes no bind parameter, so the SCRAM verifier is a literal in the statement;
            # statement logging is switched off for this one transaction (superuser, SET LOCAL) so that log_statement,
            # log_min_duration_statement or a failing statement cannot write the verifier to the server log
            with quiet(admin):
                admin.execute(sql.SQL("alter role {} password {}").format(sql.Identifier(r), sql.Literal(h)))
            p = pathlib.Path(config.pgpass(r))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"{config.PG_HOST}:{config.PG_PORT}:*:{r}:{pw}\n".encode())
            finally:
                os.close(fd)
            os.chmod(p, 0o600)
            del pw
    for r in db.REVIEWERS:
        admin.execute(sql.SQL("grant {} to {}").format(sql.Identifier(db.REVIEWER_GROUP), sql.Identifier(r)))
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
    admin.execute(f'grant connect on database "{config.EVENTS_DB}" to wp_app, wp_verifier, wp_legal, wp_broker, wp_sealer, ' + ', '.join(db.REVIEWERS))
    admin.execute(f'revoke connect on database "{config.KEYS_DB}" from public')
    admin.execute(f'grant connect on database "{config.KEYS_DB}" to wp_kms, wp_verifier')
    for d in (config.EVENTS_DB, config.KEYS_DB):      # review J M5: no role needs temp tables; another session's temp
        admin.execute(f'revoke temporary on database "{d}" from public')   # table would hold every erasure open
    admin.execute("grant pg_checkpoint to wp_kms with inherit true")
    admin.execute("grant pg_read_all_stats to wp_kms with inherit true")   # purge() reads other backends' xid/xmin
    # item 1 (4960f50 round): on PG 16 no setting bounds a transaction that keeps issuing short statements
    # (transaction_timeout arrives in PG 17); the watchdog terminates such backends (wp/xact_watchdog.py)
    admin.execute("grant pg_signal_backend, pg_read_all_stats to wp_watchdog with inherit true")
    ev, ks = db.admin(), db.admin(db=config.KEYS_DB)
    ks.execute("grant execute on function pg_switch_wal() to wp_kms")
    if reset:
        ev.execute("drop schema public cascade"); ev.execute("create schema public authorization wp_owner")
        # DESIGN-NOTES A8: the seal key's private schema is created by the admin next to public and dropped with it
        ev.execute("drop schema if exists wp_seal cascade"); ev.execute("create schema wp_seal authorization wp_owner")
        ev.execute("set role wp_owner"); ev.execute((HERE / "schema.sql").read_text()); ev.execute("reset role")
        ks.execute("drop schema public cascade"); ks.execute("create schema public authorization wp_kms")
        ks.execute("set role wp_kms"); ks.execute((HERE / "keys_schema.sql").read_text()); ks.execute("reset role")
    ev.execute("set role wp_owner"); registry.sync_db(ev); ev.execute("reset role")
    _seal_key(ev)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("setup ok", "reset" if reset else "")


def _seal_key(ev):
    """The seal authority's key (DESIGN-NOTES A2, A4 KEY HANDLING): generated once into WP_SECRETS_DIR/seal_key (0600,
    never overwritten, never printed), its pads installed in wp_seal.seal_keys as wp_owner when that key id is absent.
    Prints only the key id (a random label). The authority's deployment copies the file to its own 0700 directory."""
    from . import sealer
    p = sealer.key_path()
    if not p.exists():
        sealer.new_key_file(p)
    kid, key = sealer.load_key(p)
    with quiet(ev):                      # review 42cfd11 R7: a successful install logs no pad either
        ev.execute("set local role wp_owner")
        if ev.execute("select 1 from wp_seal.seal_keys where key_id = %s", (kid,)).fetchone() is None:
            ip, op = sealer.pads(key)
            ev.execute("select wp_seal.install(%s, %s, %s)", (kid, ip, op))
    del key
    print("seal key", kid)


if __name__ == "__main__":
    main("--reset" in sys.argv)
