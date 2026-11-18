"""Migration of a decision-core database from the pre-R3-7 schema (pol/pgcore.py at 5a80e52: app-writable tables plus
dc.apply_authority) to the definer core (pol/_pgcore.sql, 27ad63a), with a rollback. Admin path: run as the database superuser.

forward(conn, tenants, idp_password_file)   ONE transaction; on any check failure nothing changes.
  1. Preconditions: the old schema is present (dc.apply_authority exists), no dc_pre_r3_7 schema exists, every tenant's chain verifies.
  2. The old schema is renamed dc_pre_r3_7 and dc_app loses USAGE on it (the R3-7 hole stays closed during the rollback window).
  3. The definer core is installed and each tenant's genesis is written from `tenants` (the same configs the deployment was installed
     with: the genesis is not recoverable from the old tables, which hold only current state). The roles the core needs and the old
     schema lacks are created first: dc_idp, and dc_evictor (NOLOGIN, member of pg_signal_backend only: it owns dc._evict,
     FINAL-JUDGMENT.md 4.4). The core makes the pg_catalog advisory-lock functions non-PUBLIC in this database (4.4).
  4. REPLAY, not copy: every old event, in chain order, goes through the owner-only raw core (dc._lock_event, dc._reduce_apply). Its
     new outcome code must equal the stored one. An event the new core judges differently (for example a grant row dc_app inserted
     directly in a non-admin's name) makes the migration refuse: state derived from such a log is never carried over. LIMIT: a forged
     row in the name of a principal who HAD the authority is indistinguishable from a genuine one (the old schema kept no session
     provenance); the operator must review the log of the R3-7 window if dc_app credentials may have leaked.
  5. The rebuilt chain must equal the old one hash for hash (same chain formula, same events), so an anchor exported before the
     migration still checks after it (LIMIT, R3-8: an anchor is only as strong as its storage; FileAnchor is an append-only file, not
     immutable storage, and events after the last export are protected only by the in-database chain). The rebuilt state (principals, roles, heads, proposals) must equal the old tables: a difference
     means the old state was written outside the event log (R3-7: dc_app could update heads directly) and the migration refuses.
  6. Carried over verbatim: the vault (dc.vals), the subject-token mapping (dc.subject_tokens) and the audit history (dc.audit).
     Not carried: dc.auth_applied (gone with dc.apply_authority). Sessions start empty: every client authenticates again.
  7. At the end, a DATA MARK of the migrated schema is stored in dc.migration_mark: per table of schema dc, the row count and a hash of
     the rows (dc.sessions excluded: logins are not data, and every client authenticates again after a rollback anyway).
rollback(conn)   ONE transaction: locks every table of the migrated schema (EXCLUSIVE: no write can land between the check and the
  drop), recomputes the data mark and REFUSES when it differs from the mark stored at the end of forward, that is when anything was
  written after the migration (an applied or refused request, an erasure, an audit row): dropping the schema would discard those
  writes, or undo an erasure (review-3 N4). There is no override: carry such writes back by hand or keep the migrated schema (finish).
  Otherwise it drops the new dc schema (and dc_crypto, which the old schema did not have), renames dc_pre_r3_7 back to dc and restores
  dc_app's USAGE, and restores the EXECUTE grants of the pg_catalog advisory-lock functions exactly as forward recorded them before it
  installed the core (review of 2262eaa F8; the old engine takes its idempotency lock as dc_app with a raw pg_advisory_xact_lock, which
  PostgreSQL's default, PUBLIC, allows). A mark written by an older forward has no recorded grants: then PUBLIC gets EXECUTE back (the
  default), as before. Roles dc_idp and dc_evictor are left in place (roles are cluster-wide; the old schema references none of their
  grants). Refused when dc_pre_r3_7 or the mark is absent.
finish(conn)   drops dc_pre_r3_7 after the deployment has been accepted; there is no rollback after finish.

usage (lab host): python -m ops.migrate_r3_7 forward|rollback|finish --tenants <json file>  (env: POL_PG_PORT, POL_PG_SUPER_PASSFILE,
POL_PG_IDP_PASSWORD_FILE as file paths; POL_PG_DB names the database)."""
import hashlib
import json
import os
import sys

from psycopg import sql
from psycopg.types.json import Jsonb

from pol import _core, pgcore

PRE = "dc_pre_r3_7"
STATE_TABLES = ("principal", "role", "head", "props")
# Logins are not decision data: sessions, enrolled factors and the replay ledger exist only in the migrated schema (the pre-R3-7 schema
# has no session path), so a rollback that drops them loses nothing the old schema could hold; enrolled devices re-enrol after a
# re-migration (step 2). A proof used before a rollback could be replayed after a re-migration until it expires (at most 300 s).
MARK_EXCLUDED = ("sessions", "factors", "used_proofs", "migration_mark")


class MigrationRefused(Exception):
    pass


# the pg_catalog advisory-lock functions (FINAL-JUDGMENT.md 4.4); the same selection as the DO block at the end of pol/_pgcore.sql
ADVISORY = ("select p.oid::regprocedure::text from pg_catalog.pg_proc p where p.pronamespace = 'pg_catalog'::regnamespace"
            " and p.proname like 'pg\\_%advisory%'")


def advisory_to_public(c):
    """PostgreSQL's default: PUBLIC may EXECUTE the advisory-lock functions. The definer core revokes that in its database; the
    pre-R3-7 engine needs it (its dc_app connection calls pg_advisory_xact_lock itself)."""
    for (f,) in c.execute(ADVISORY).fetchall():
        c.execute(sql.SQL("grant execute on function {} to public").format(sql.SQL(f)))


def advisory_acl(c):
    """{function: [[grantee, grantable], ...]} of EXECUTE on the advisory-lock functions, the owner left out ("public" for PUBLIC)."""
    rows = c.execute("select p.oid::regprocedure::text, case when a.grantee = 0 then 'public' else r.rolname end, a.is_grantable"
                     " from pg_catalog.pg_proc p cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a"
                     " left join pg_catalog.pg_roles r on r.oid = a.grantee"
                     " where p.pronamespace = 'pg_catalog'::regnamespace and p.proname like 'pg\\_%advisory%'"
                     " and a.privilege_type = 'EXECUTE' and a.grantee <> p.proowner").fetchall()
    out = {}
    for f, g, opt in rows:
        out.setdefault(f, []).append([g, bool(opt)])
    for (f,) in c.execute(ADVISORY).fetchall():
        out.setdefault(f, [])
    return {f: sorted(v) for f, v in out.items()}


def restore_advisory_acl(c, acl):
    """Make the EXECUTE grants of each advisory-lock function exactly `acl` (as advisory_acl returns it): revoke every grantee that is
    not in it, grant every one that is (a role dropped since forward is skipped).
    Review of 6b77354 N7: `acl` comes from dc.migration_mark, which dc_owner can rewrite, and this runs as superuser, so it is
    validated first: every function it names must be one of the live advisory-lock signatures (read from pg_proc here; only those
    strings are ever placed in SQL text), and every entry must be [role name, bool]. Anything else refuses the rollback."""
    now = advisory_acl(c)
    if not isinstance(acl, dict):
        raise MigrationRefused("dc.migration_mark.advisory_acl is not an object: refusing to restore from it")
    bad = sorted(f for f in acl if f not in now)
    if bad:
        raise MigrationRefused("dc.migration_mark.advisory_acl names functions that are not advisory-lock functions: %d entries"
                               % len(bad))
    for f, v in acl.items():
        if not isinstance(v, list) or not all(isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) and isinstance(e[1], bool)
                                              for e in v):
            raise MigrationRefused("dc.migration_mark.advisory_acl has a malformed entry: refusing to restore from it")
    for f in sorted(now):
        want = {g: opt for g, opt in acl.get(f, [])}
        for g, _opt in now.get(f, []):
            if g not in want:
                c.execute(sql.SQL("revoke execute on function {} from {} cascade").format(
                    sql.SQL(f), sql.SQL("public") if g == "public" else sql.Identifier(g)))
        for g, opt in sorted(want.items()):
            if g != "public" and c.execute("select 1 from pg_roles where rolname = %s", (g,)).fetchone() is None:
                continue
            c.execute(sql.SQL("grant execute on function {} to {}" + (" with grant option" if opt else "")).format(
                sql.SQL(f), sql.SQL("public") if g == "public" else sql.Identifier(g)))


def _exists_schema(c, name):
    return c.execute("select 1 from pg_namespace where nspname = %s", (name,)).fetchone() is not None


def _old_state(c, schema, tenant):
    q = sql.Identifier(schema)
    return {
        "principal": sorted(c.execute(sql.SQL("select pid, kind, admin, revoked from {}.principal where tenant=%s").format(q), (tenant,)).fetchall()),
        "role": sorted(c.execute(sql.SQL("select req, pid, role from {}.role where tenant=%s").format(q), (tenant,)).fetchall()),
        # open_n is a derived counter whose meaning changed in round 3 (non-human items only): rebuilt, not compared
        "head": sorted((r, json.dumps(d, sort_keys=True)) for r, d in
                       c.execute(sql.SQL("select req, doc from {}.head where tenant=%s").format(q), (tenant,)).fetchall()),
        "props": sorted((r, i, k, a, s, json.dumps(d, sort_keys=True)) for r, i, k, a, s, d in
                        c.execute(sql.SQL("select req, id, kind, author, status, doc from {}.props where tenant=%s").format(q), (tenant,)).fetchall()),
    }


def _chain(c, schema, tenant):
    return c.execute(sql.SQL("select chain_n, prev_hash, hash from {}.events where tenant=%s order by chain_n").format(sql.Identifier(schema)),
                     (tenant,)).fetchall()


def _dc_tables(c):
    return [t for (t,) in c.execute("select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                    "where n.nspname = 'dc' and c.relkind = 'r' order by 1").fetchall()]


def _data_mark(c):
    """{table: [rows, sha256 of the sorted row texts]} for every table of schema dc except MARK_EXCLUDED."""
    out = {}
    for t in _dc_tables(c):
        if t in MARK_EXCLUDED:
            continue
        rows = c.execute(sql.SQL("select row_to_json(x)::text from dc.{} x").format(sql.Identifier(t))).fetchall()
        out[t] = [len(rows), hashlib.sha256("\n".join(sorted(r[0] for r in rows)).encode()).hexdigest()]
    return out


def _genesis(c, tenants):
    for t, cfg in tenants.items():
        st = _core.MemoryState(cfg)
        c.execute("insert into dc.tenant(tenant) values (%s)", (t,))
        c.execute("insert into dc.chainhead(tenant, hash) values (%s, %s)", (t, "0" * 64))
        for pid, kind in st.principals.items():
            c.execute("insert into dc.principal values (%s,%s,%s,%s,false)", (t, pid, kind, pid in st.admins))
        for req in sorted(st.heads):
            for pid in sorted(st.authority[req]):
                c.execute("insert into dc.role values (%s,%s,%s,'decide')", (t, req, pid))
            for pid in sorted(st.proposers[req]):
                c.execute("insert into dc.role values (%s,%s,%s,'propose')", (t, req, pid))
            c.execute("insert into dc.head(tenant, req, label, doc) values (%s,%s,%s,%s)", (t, req, Jsonb(st.req_labels[req]), Jsonb(st.heads[req])))


def forward(c, tenants, idp_password_file):
    """Returns a report dict; raises MigrationRefused (and changes nothing) when a check fails."""
    report = {"tenants": {}}
    with c.transaction():
        if not _exists_schema(c, "dc") or c.execute("select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                                                    "where n.nspname = 'dc' and p.proname = 'apply_authority'").fetchone() is None:
            raise MigrationRefused("no pre-R3-7 schema dc (dc.apply_authority not found)")
        if _exists_schema(c, PRE):
            raise MigrationRefused("schema %s exists: a previous migration was not finished or rolled back" % PRE)
        old_tenants = sorted(r[0] for r in c.execute("select tenant from dc.tenant").fetchall())
        if old_tenants != sorted(tenants):
            raise MigrationRefused("tenant configs %s do not match the installed tenants %s" % (sorted(tenants), old_tenants))
        for t in old_tenants:
            n, ok = c.execute("select n, ok from dc.verify_chain(%s)", (t,)).fetchone()
            if not ok:
                raise MigrationRefused("tenant %s: the old chain does not verify" % t)
        before = {t: (_old_state(c, "dc", t), _chain(c, "dc", t)) for t in old_tenants}
        acl_before = advisory_acl(c)                      # F8: restored exactly by rollback
        public_create = c.execute("select has_schema_privilege('public', 'public', 'CREATE')").fetchone()[0]
        c.execute(sql.SQL("alter schema dc rename to {}").format(sql.Identifier(PRE)))
        c.execute(sql.SQL("revoke usage on schema {} from dc_app").format(sql.Identifier(PRE)))
        if c.execute("select 1 from pg_roles where rolname='dc_idp'").fetchone() is None:
            c.execute("create role dc_idp login")
        if c.execute("select 1 from pg_roles where rolname='dc_evictor'").fetchone() is None:     # 4.4, as pgcore.install
            c.execute("create role dc_evictor nologin")
        c.execute("grant pg_signal_backend to dc_evictor")
        c.execute(sql.SQL("alter role dc_idp with login password {}").format(sql.Literal(pgcore._scram_verifier(pgcore._read_secret(idp_password_file)))))
        with open(pgcore._SQL_PATH, encoding="utf-8") as f:
            c.execute(f.read())
        c.execute("reset role")
        _genesis(c, tenants)
        for t in old_tenants:
            evs = c.execute(sql.SQL("select seq, code, ev from {}.events where tenant=%s order by chain_n").format(sql.Identifier(PRE)), (t,)).fetchall()
            for seq, code, ev in evs:
                c.execute("select dc._lock_event(%s, %s)", (t, Jsonb(ev)))
                r = c.execute("select dc._reduce_apply(%s, %s)", (t, Jsonb(ev))).fetchone()[0]
                if r["code"] != code:
                    raise MigrationRefused("tenant %s: old event seq %d (%s) stored as %s, judged %s by the definer core: the log holds an event "
                                           "the rules refuse (forged or inserted outside the core)" % (t, seq, ev.get("id"), code, r["code"]))
            old_state, old_chain = before[t]
            if _chain(c, "dc", t) != old_chain:
                raise MigrationRefused("tenant %s: the rebuilt chain differs from the old chain" % t)
            new_state = _old_state(c, "dc", t)
            diff = [k for k in STATE_TABLES if new_state[k] != old_state[k]]
            if diff:
                raise MigrationRefused("tenant %s: state rebuilt from the log differs from the old tables in %s: the old state was written "
                                       "outside the event log" % (t, diff))
            report["tenants"][t] = {"events_replayed": len(evs), "chain_head": old_chain[-1][2] if old_chain else None}
        for tbl, cols in (("vals", "tenant, ref, text, label, subjects"), ("subject_tokens", "tenant, subject, token"),
                          ("audit", "n, tenant, principal, code, original, ev")):
            c.execute(sql.SQL("insert into dc.{} (" + cols + ") select " + cols + " from {}.{}").format(sql.Identifier(tbl), sql.Identifier(PRE),
                                                                                                        sql.Identifier(tbl)))
            report[tbl] = c.execute(sql.SQL("select count(*) from dc.{}").format(sql.Identifier(tbl))).fetchone()[0]
        c.execute("select setval(pg_get_serial_sequence('dc.audit', 'n'), greatest((select max(n) from dc.audit), 1))")
        if not public_create:
            c.execute("revoke create on schema public from public")
        c.execute("insert into dc.audit(tenant, principal, code, ev) values (null, null, 'migrated_r3_7', %s)", (Jsonb(report),))
        c.execute("create table dc.migration_mark(id int primary key check (id = 1), mark jsonb not null, advisory_acl jsonb)")
        c.execute("revoke all on dc.migration_mark from public")
        c.execute("insert into dc.migration_mark values (1, %s, %s)", (Jsonb(_data_mark(c)), Jsonb(acl_before)))
    return report


def rollback(c):
    with c.transaction():
        if not _exists_schema(c, PRE):
            raise MigrationRefused("no %s schema: nothing to roll back (finished, or never migrated)" % PRE)
        if c.execute("select to_regclass('dc.migration_mark')").fetchone()[0] is None:
            raise MigrationRefused("no dc.migration_mark: cannot prove the migrated schema is unwritten, so rollback could discard data")
        c.execute(sql.SQL("lock table {} in exclusive mode").format(sql.SQL(", ").join(sql.SQL("dc.") + sql.Identifier(t) for t in _dc_tables(c))))
        mark, acl = c.execute("select mark, to_jsonb(m) -> 'advisory_acl' from dc.migration_mark m where id = 1").fetchone()
        now = _data_mark(c)
        changed = sorted(t for t in set(mark) | set(now) if mark.get(t) != now.get(t))
        if changed:
            raise MigrationRefused("written after the migration (%s): a rollback would discard those writes or undo an erasure; carry "
                                   "them back by hand or keep the migrated schema" % ", ".join(changed))
        c.execute("drop schema dc cascade")
        c.execute("drop schema if exists dc_crypto cascade")
        c.execute(sql.SQL("alter schema {} rename to dc").format(sql.Identifier(PRE)))
        c.execute("grant usage on schema dc to dc_app")
        if acl is None:
            advisory_to_public(c)                         # a mark from an older forward: PostgreSQL's default
        else:
            restore_advisory_acl(c, acl)
    return {"rolled_back": True}


def finish(c):
    with c.transaction():
        if not _exists_schema(c, PRE):
            raise MigrationRefused("no %s schema" % PRE)
        c.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(PRE)))
    return {"finished": True}


def main(argv):
    if len(argv) < 2 or argv[1] not in ("forward", "rollback", "finish"):
        sys.exit(__doc__)
    c = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], int(os.environ["POL_PG_PORT"]), "pol-migrate")
    try:
        if argv[1] == "forward":
            if "--tenants" not in argv:
                sys.exit("forward needs --tenants <json file of {tenant: config}>")
            with open(argv[argv.index("--tenants") + 1], encoding="utf-8") as f:
                tenants = json.load(f)
            out = forward(c, tenants, os.environ["POL_PG_IDP_PASSWORD_FILE"])
        else:
            out = (rollback if argv[1] == "rollback" else finish)(c)
        print(json.dumps(out, sort_keys=True))
    except MigrationRefused as e:
        sys.exit("REFUSED: %s" % e)
    finally:
        c.close()


if __name__ == "__main__":
    main(sys.argv)
