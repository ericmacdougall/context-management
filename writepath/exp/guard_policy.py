"""The committed expectation of the live catalogs: what wp/schema.sql, wp/keys_schema.sql and wp/setup.py produce on a
clean cluster, as rules. One source for exp/guard_check.py (the batch's guard check) and exp/disk_gate.py (the
gate's per-run catalog check, GATE-DESIGN 9). No imports: the gate reads it without a database driver.

tests/test_disk_gate.py binds the cluster half to wp/setup.py and wp/db.py by reading their text (roles, timeouts,
memberships, database grants) and the privilege half to wp/schema.sql (table, schema and function grants), so an edit
of setup.py or schema.sql that is not mirrored here fails a test.

Privileges here are EFFECTIVE privileges, as has_table_privilege, has_schema_privilege and has_function_privilege
report them: a member of wp_reviewer holds the group's privileges (inherit), and a grant to PUBLIC counts for every
role. Superusers and the owner role are not listed (they hold everything)."""

S, I, U, D = "SELECT", "INSERT", "UPDATE", "DELETE"
# G1, G2: every base table of the events database and the app role's table privileges on it
POLICY = {
    "events": {S, I}, "quarantine": {S, I},
    "inbox": {S, I, U}, "analyst_jobs": {S, I, U}, "facts": {S, I, U}, "cursors": {S, I, U},
    "erasure_jobs": {S, I},              # plus column-level UPDATE of runner fields (checked separately)
    "tombstones": {S}, "store_registry": {S}, "decision_heads": {S}, "chains": {S}, "subjects": {S},
    "leases": set(), "quarantine_quota": set(),
    "identifier_epochs": {S}, "subject_identifiers": {S}, "erasure_mentions": {S}, "legal_holds": {S}, "erasure_withheld": {S}, "erasure_rescanned": {S},
    # hardening (feat/writepath-hardening): written only through SECURITY DEFINER functions, never by the app role
    "write_refusals": set(),             # m5 b1: wp_write_refused counts refused writes
    "identifier_proposals": set(),       # D3: wp_identifier_propose, approve, reject
    "quarantine_removed": set(),         # review J N1: the erasure cascade records removed quarantine rows
}
COLUMN_UPDATE_OK = ("erasure_jobs",)   # the only table without table-level UPDATE where column UPDATE is allowed
PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
ROW, BEFORE, INS, DEL, UPD, TRUNC = 1, 2, 4, 8, 16, 32           # pg_trigger.tgtype bits
UNIQUE_INDEXES = ("decision_source_once", "erasure_one_open")    # G7
APP_NO_EXECUTE = ("wp_hold_place", "wp_hold_release")            # D1: holds are the legal role's alone
EVENTS_OWNER, KEYS_OWNER = "wp_owner", "wp_kms"                   # G1, G5
# G3 exemption. ASSUMPTION (inferred from wp/schema.sql at e1e7e51; not decided by the hardening owner, reported to
# them): schema.sql creates write_refusals without guard triggers (it is in neither trigger list). It is admitted on
# compensating rules, all checked: owner wp_owner; the effective privileges below (the verifier reads it, no role
# writes it); its writer function (SECURITY DEFINER, owner wp_owner, EXECUTE for the app role alone, EXECUTE_ONLY);
# and any trigger it does carry is enabled ALWAYS. Not covered: wp_owner (a NOLOGIN role) truncating it.
UNGUARDED_OK = {"write_refusals": "wp_write_refused"}

# G4 and the cluster (wp/setup.py): roles, their attributes and settings, memberships, database grants
OWNER_ROLE = "wp_owner"
LOGIN_ROLES = ("wp_app", "wp_verifier", "wp_kms", "wp_legal", "wp_watchdog", "wp_broker",
               "wp_sealer", "wp_rev1", "wp_rev2")                  # hardening: the seal authority; D3 reviewers
GROUP_ROLES = ("wp_reviewer",)          # D3: NOLOGIN with ROLE_FLAGS, no settings, no CONNECT; members wp_rev1, wp_rev2
ROLE_FLAGS = {"rolsuper": False, "rolinherit": True, "rolcreaterole": False, "rolcreatedb": False,
              "rolreplication": False, "rolbypassrls": False}
ROLE_SETTINGS = {"statement_timeout": "30s", "idle_in_transaction_session_timeout": "5s", "lock_timeout": "10s"}
MEMBERSHIPS = {("pg_checkpoint", "wp_kms"), ("pg_read_all_stats", "wp_kms"), ("pg_signal_backend", "wp_watchdog"),
               ("pg_read_all_stats", "wp_watchdog"),
               ("wp_reviewer", "wp_rev1"), ("wp_reviewer", "wp_rev2")}   # (granted role, member)
CONNECT = {"events": ("wp_app", "wp_verifier", "wp_legal", "wp_broker", "wp_sealer", "wp_rev1", "wp_rev2"),
           "keys": ("wp_kms", "wp_verifier")}
# review J M5 (setup.py: revoke temporary from public): no grantee but the database owner and the admin holds
# TEMPORARY on a stream database
TEMPORARY = {"events": (), "keys": ()}
SYSTEM_DATABASES = ("postgres", "template0", "template1")
# host/bootstrap.py (the legacy home old_repro runs, and the node's settings): login roles with ROLE_FLAGS and no
# settings, their databases (name: owner), one membership, and the ALTER SYSTEM values (name: value as written).
# Allowed, never required (wp.setup does not create them); any other value or object is a reason.
LEGACY_ROLES = ("emlab", "kms")
LEGACY_DATABASES = {"emlab": "emlab", "keys": "kms"}
LEGACY_MEMBERSHIPS = {("pg_checkpoint", "kms")}
AUTO_CONF = {"max_wal_size": "256MB", "min_wal_size": "64MB", "wal_recycle": "off", "wal_keep_size": "0",
             "archive_mode": "off", "max_connections": "200"}

# Namespaces (other than pg_* and information_schema) of each stream database, with their owners. The private seal
# schema (DESIGN-NOTES "Seal authority" A3, A8) holds exactly these tables and functions, all owned by wp_owner, none
# SECURITY DEFINER; no role holds a privilege on the schema, its tables or its functions (SCHEMA_PRIVS, EXECUTE_ONLY,
# and no role in ROLE_TABLE_PRIVS names a wp_seal table). Snapshot rows never carry the key pads.
NAMESPACES = {"events": {"public": "wp_owner", "wp_seal": "wp_owner"}, "keys": {"public": "wp_kms"}}
SEAL_SCHEMA = "wp_seal"
SEAL_TABLES = ("seal_keys",)
SEAL_FUNCTIONS = ("install", "message", "verify")

_REVIEWERS = ("wp_reviewer", "wp_rev1", "wp_rev2")
# Effective table privileges on the events database's tables (schema qualified) of every committed role but wp_app
# (POLICY, G2): a role or table not named holds none. VERIFIER_READS_PUBLIC holds SELECT on every public table and
# nothing else (schema.sql: grant select on all tables in schema public to wp_verifier).
VERIFIER_READS_PUBLIC = "wp_verifier"
ROLE_TABLE_PRIVS = {
    "wp_legal": {"public.legal_holds": {S}, "public.erasure_jobs": {S}, "public.erasure_withheld": {S}},
    "wp_sealer": {"public.identifier_epochs": {S}, "public.subject_identifiers": {S}},
    **{r: {"public.identifier_proposals": {S}, "public.subjects": {S}} for r in _REVIEWERS},
}
# Effective schema privileges on the events database's schemas: exactly these roles (wp_owner and superusers aside)
SCHEMA_PRIVS = {("public", "USAGE"): {"wp_app", "wp_verifier", "wp_legal", "wp_broker", "wp_sealer", *_REVIEWERS},
                ("public", "CREATE"): set(), ("wp_seal", "USAGE"): set(), ("wp_seal", "CREATE"): set()}
# Functions whose EXECUTE is held by exactly these roles (effective; schema qualified); other functions are not judged
EXECUTE_ONLY = {
    "public.wp_identifier_approve": set(_REVIEWERS), "public.wp_identifier_reject": set(_REVIEWERS),
    "public.wp_identifier_propose": {"wp_app", *_REVIEWERS},
    "public.wp_identifier_declare": set(),               # D3: internal, no role may call it
    "public.wp_hold_place": {"wp_legal"}, "public.wp_hold_release": {"wp_legal"},
    "public.wp_erasure_complete": {"wp_verifier"},
    "public.wp_write_refused": {"wp_app"},
    "public.wp_lease_holder_current": {"wp_broker"},     # D6: the broker's only grant
    **{f"wp_seal.{f}": set() for f in SEAL_FUNCTIONS},
}

# Service containers (hardening D2, D6: exp/h_d2_uids.py, exp/h_d6_broker.py start them under the stream's container
# prefix with the stream label, mount parts of WP_HOME, and remove them before they end). The rule is the prefix and
# the label: at admission no container but the run's two stores may carry either, and at the end none may remain.
# The names give the reason its text. The seal authority has no container in this tree (wp/sealer.py describes uid
# 20005 and /svc/sealer only): "sealer" is ASSUMPTION, and the prefix rule covers whatever name it gets.
STREAM_LABEL = ("emlab2.stream", "wphard")
SERVICE_CONTAINERS = {"keysvc": "the key service", "verifier": "the verifier service", "broker": "the lease broker",
                      "app": "an application container", "app12": "an application container (D6 C12)",
                      "peer": "a peer container (D6)", "sealer": "the seal authority (ASSUMPTION: name not in tree)"}
