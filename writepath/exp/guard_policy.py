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
# G3 without guard triggers, by design (the hardening owner, 2026-11-18, relayed by main: write_refusals holds counts
# only, codes WPM01-03 under a check constraint, and its only writer is wp_write_refused). In place of guard triggers
# the gate requires exactly, at admission and at the end:
#   R1 no login role (superusers aside: they hold every privilege) holds a privilege on the table, table or column
#      level, effective, but UNGUARDED_READ;
#   R2 the writer is SECURITY DEFINER and owned by wp_owner;
#   R3 the writer's proconfig pins search_path, exactly UNGUARDED[t]["config"];
#   R4 EXECUTE on the writer is held by exactly UNGUARDED[t]["execute"] (effective, every non-superuser role) and its
#      ACL grants nothing to PUBLIC (a NULL ACL is the default one, which grants EXECUTE to PUBLIC).
# Any trigger it does carry must be enabled ALWAYS. Not covered: wp_owner (a NOLOGIN role) or a superuser writing it.
UNGUARDED = {"write_refusals": {"writer": "wp_write_refused", "config": "search_path=public, pg_temp",
                                "execute": ("wp_app",)}}
UNGUARDED_OK = {t: v["writer"] for t, v in UNGUARDED.items()}     # the names guard_check and G3 coverage skip
# TARGET_MISMATCH (not the owner's words): the owner said no login role holds any privilege on write_refusals, but
# wp/schema.sql (`grant select on all tables in schema public to wp_verifier`, after the table is created) gives
# wp_verifier SELECT on it, and the live cluster shows it (t2-dev, 2026-11-18). The gate admits that one grant, named
# here; with a revoke in schema.sql this becomes {} and the gate then refuses it.
UNGUARDED_READ = {"write_refusals": {"wp_verifier": {"SELECT"}}}

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

# Service containers (the hardening owner, 2026-11-18, relayed by main). No standing service container: by default the
# key service and the seal authority run in-process under the experiment's uid. Socket mode exists only inside steps
# (SU, D6, h_d2) as short-lived containers the step removes: image ubuntu:24.04, fixed uids, host network, 512 MiB,
# the stream label, and exactly the bind mounts of exp/h_d2_uids.py _run_args. The gate's rule:
#   at admission no container but the run's two stores may carry the stream prefix or label (BLOCKED);
#   during the run every container created with the prefix or the label, or renamed or updated to carry them, must
#   be one of SERVICE_CONTAINERS by exact name and match its spec, inspected when Docker reports its creation (VOID);
#   at the end none may remain (VOID).
# Their bind mounts under WP_HOME (conf, logs, svc read-only; run read-write) are the committed mount list, admitted
# only during the run; the C12 rule (no container but the stores uses this home at admission or end) is unchanged.
# The gate checks the mount list, not the files in it (LIMITS).
STREAM_LABEL = ("emlab2.stream", "wphard")
SERVICE_IMAGE, SERVICE_MEMORY, SERVICE_NETWORK = "ubuntu:24.04", 512 * 1024 ** 2, "host"
SERVICE_CONTAINERS = {                       # name after the stream prefix: (uid, what it is)
    "keysvc": (20003, "the key service (SU, D6, h_d2)"), "sealer": (20005, "the seal authority (SU)"),
    "broker": (20004, "the lease broker (D6)"), "app": (20001, "a one-shot application client"),
    "peer": (20002, "a one-shot peer client (SU, D6)"), "verifier": (20002, "the verifier client (h_d2)"),
    # ASSUMPTION (not in the owner's list): exp/h_d6_broker.py starts emlab2-wphard-app12, uid 20001, the same layout
    "app12": (20001, "an application client (D6 C12)"),
}
# (source, destination, read-write): "home:<dir>" is WP_HOME/<dir>, "tree" the run's pinned tree, else an absolute path
SERVICE_MOUNTS = (("/usr", "/usr", False), ("home:venv", "/venv", False), ("tree", "/src", False),
                  ("home:conf", "/wp/conf", False), ("home:logs", "/wp/logs", False), ("home:svc", "/svc", False),
                  ("home:run", "/run/wphard", True))
