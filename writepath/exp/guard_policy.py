"""The committed expectation of the live catalogs: what wp/schema.sql, wp/keys_schema.sql and wp/setup.py produce on a
clean cluster, as rules. One source for exp/guard_check.py (the batch's guard check) and exp/disk_gate.py (the
gate's per-run catalog check, GATE-DESIGN 9). No imports: the gate reads it without a database driver.

tests/test_disk_gate.py binds the cluster half to wp/setup.py and wp/db.py by reading their text (roles, timeouts,
memberships, database grants), so an edit of setup.py that is not mirrored here fails a test."""

S, I, U, D = "SELECT", "INSERT", "UPDATE", "DELETE"
# G1, G2: every base table of the events database and the app role's table privileges on it
POLICY = {
    "events": {S, I}, "quarantine": {S, I},
    "inbox": {S, I, U}, "analyst_jobs": {S, I, U}, "facts": {S, I, U}, "cursors": {S, I, U},
    "erasure_jobs": {S, I},              # plus column-level UPDATE of runner fields (checked separately)
    "tombstones": {S}, "store_registry": {S}, "decision_heads": {S}, "chains": {S}, "subjects": {S},
    "leases": set(), "quarantine_quota": set(),
    "identifier_epochs": {S}, "subject_identifiers": {S}, "erasure_mentions": {S}, "legal_holds": {S}, "erasure_withheld": {S}, "erasure_rescanned": {S},
}
COLUMN_UPDATE_OK = ("erasure_jobs",)   # the only table without table-level UPDATE where column UPDATE is allowed
PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
ROW, BEFORE, INS, DEL, UPD, TRUNC = 1, 2, 4, 8, 16, 32           # pg_trigger.tgtype bits
UNIQUE_INDEXES = ("decision_source_once", "erasure_one_open")    # G7
APP_NO_EXECUTE = ("wp_hold_place", "wp_hold_release")            # D1: holds are the legal role's alone
EVENTS_OWNER, KEYS_OWNER = "wp_owner", "wp_kms"                   # G1, G5

# G4 and the cluster (wp/setup.py): roles, their attributes and settings, memberships, database grants
OWNER_ROLE = "wp_owner"
LOGIN_ROLES = ("wp_app", "wp_verifier", "wp_kms", "wp_legal", "wp_watchdog", "wp_broker")
ROLE_FLAGS = {"rolsuper": False, "rolinherit": True, "rolcreaterole": False, "rolcreatedb": False,
              "rolreplication": False, "rolbypassrls": False}
ROLE_SETTINGS = {"statement_timeout": "30s", "idle_in_transaction_session_timeout": "5s", "lock_timeout": "10s"}
MEMBERSHIPS = {("pg_checkpoint", "wp_kms"), ("pg_read_all_stats", "wp_kms"), ("pg_signal_backend", "wp_watchdog"),
               ("pg_read_all_stats", "wp_watchdog")}                 # (granted role, member)
CONNECT = {"events": ("wp_app", "wp_verifier", "wp_legal", "wp_broker"), "keys": ("wp_kms", "wp_verifier")}
SYSTEM_DATABASES = ("postgres", "template0", "template1")
# host/bootstrap.py (the legacy home old_repro runs, and the node's settings): login roles with ROLE_FLAGS and no
# settings, their databases (name: owner), one membership, and the ALTER SYSTEM values (name: value as written).
# Allowed, never required (wp.setup does not create them); any other value or object is a reason.
LEGACY_ROLES = ("emlab", "kms")
LEGACY_DATABASES = {"emlab": "emlab", "keys": "kms"}
LEGACY_MEMBERSHIPS = {("pg_checkpoint", "kms")}
AUTO_CONF = {"max_wal_size": "256MB", "min_wal_size": "64MB", "wal_recycle": "off", "wal_keep_size": "0",
             "archive_mode": "off", "max_connections": "200"}
