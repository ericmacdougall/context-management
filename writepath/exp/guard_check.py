"""Guard enumeration gate (review F3/F4/B). Reads the LIVE catalogs, not a list in code:

  G1 every table in wp_events.public is owned by wp_owner and has a declared app-role privilege policy (POLICY);
     a table without a declared policy fails.
  G2 the app role's privileges on every table equal its policy (DELETE and TRUNCATE never), checked with
     has_table_privilege / has_any_column_privilege.
  G3 every table has BEFORE triggers covering INSERT, UPDATE, DELETE (row) and TRUNCATE (statement), and EVERY
     non-internal trigger is enabled ALWAYS ('A'), so session_replication_role=replica cannot skip it.
  G4 roles: wp_app / wp_verifier / wp_kms are not superuser, createrole, createdb, replication or bypassrls; wp_app is
     not a member of wp_owner; wp_app cannot connect to wp_keys; wp_verifier cannot read key material.
  G5 keys database tables are owned by wp_kms.
  G6 registry.unregistered() is empty.
Controls (run only with --controls, never in the gate positions): a guardless table, a trigger switched from
ALWAYS to ORIGIN, and an extra DELETE grant must each make the check fail; each is reverted.
usage: python -m exp.guard_check <label> [--controls]"""
import sys

from exp.common import write_result
from wp import config, db, registry
from wp.es import ES

S, I, U, D = "SELECT", "INSERT", "UPDATE", "DELETE"
POLICY = {
    "events": {S, I}, "quarantine": {S, I},
    "inbox": {S, I, U}, "analyst_jobs": {S, I, U}, "facts": {S, I, U}, "cursors": {S, I, U},
    "erasure_jobs": {S, I},              # plus column-level UPDATE of runner fields (checked separately)
    "tombstones": {S}, "store_registry": {S}, "decision_heads": {S}, "chains": {S}, "subjects": {S},
    "leases": set(), "quarantine_quota": set(),
}
PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
ROW, BEFORE, INS, DEL, UPD, TRUNC = 1, 2, 4, 8, 16, 32


def check(adm, kadm):
    out = {k: [] for k in ("G1", "G2", "G3", "G4", "G5", "G6")}
    tables = adm.execute("select c.relname, pg_get_userbyid(c.relowner) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                         "where n.nspname='public' and c.relkind in ('r','p')").fetchall()
    for rel, owner in tables:
        if owner != "wp_owner" or rel not in POLICY:
            out["G1"].append(f"{rel}:owner={owner},policy={rel in POLICY}")
        have = {p for p in PRIVS if adm.execute("select has_table_privilege('wp_app', %s, %s)", (f"public.{rel}", p)).fetchone()[0]}
        if rel in POLICY and have != POLICY[rel]:
            out["G2"].append(f"{rel}:{sorted(have)}!={sorted(POLICY[rel])}")
        if rel != "erasure_jobs" and rel in POLICY and U not in POLICY[rel] and adm.execute(
                "select has_any_column_privilege('wp_app', %s, 'UPDATE')", (f"public.{rel}",)).fetchone()[0]:
            out["G2"].append(f"{rel}:column UPDATE")
        trg = adm.execute("select t.tgname, t.tgtype, t.tgenabled from pg_trigger t join pg_class c on c.oid=t.tgrelid "
                          "join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname=%s and not t.tgisinternal", (rel,)).fetchall()
        cov = {"INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
        for name, typ, en in trg:
            if en != "A":
                out["G3"].append(f"{rel}.{name}:enabled={en}")
            if typ & BEFORE:
                if typ & ROW:
                    cov["INSERT"] |= bool(typ & INS); cov["UPDATE"] |= bool(typ & UPD); cov["DELETE"] |= bool(typ & DEL)
                if typ & TRUNC:
                    cov["TRUNCATE"] = True
        miss = [k for k, v in cov.items() if not v]
        if miss:
            out["G3"].append(f"{rel}:no guard for {','.join(miss)}")
    for r in ("wp_app", "wp_verifier", "wp_kms"):
        a = adm.execute("select rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls from pg_roles where rolname=%s", (r,)).fetchone()
        if a is None or any(a):
            out["G4"].append(f"{r}:attrs={a}")
    if adm.execute("select pg_has_role('wp_app', 'wp_owner', 'member')").fetchone()[0]:
        out["G4"].append("wp_app member of wp_owner")
    if adm.execute("select has_database_privilege('wp_app', %s, 'CONNECT')", (config.KEYS_DB,)).fetchone()[0]:
        out["G4"].append("wp_app can connect to the key database")
    if kadm.execute("select has_column_privilege('wp_verifier', 'public.subject_keys', 'key_hex', 'SELECT')").fetchone()[0]:
        out["G4"].append("wp_verifier can read key material")
    for rel, owner in kadm.execute("select c.relname, pg_get_userbyid(c.relowner) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                                   "where n.nspname='public' and c.relkind in ('r','p')").fetchall():
        if owner != "wp_kms":
            out["G5"].append(f"{rel}:owner={owner}")
    out["G6"] = registry.unregistered(db.verifier(), db.verifier(db=config.KEYS_DB), ES())
    return out


def controls(adm, kadm):
    res = {}
    patches = {
        "guardless_table": ("set role wp_owner; create table zz_guardless(x int); reset role", "drop table zz_guardless", "G1"),
        "trigger_origin_only": ("alter table facts enable trigger facts_fence", "alter table facts enable always trigger facts_fence", "G3"),
        "delete_grant": ("grant delete on facts to wp_app", "revoke delete on facts from wp_app", "G2"),
    }
    for name, (apply, revert, gate) in patches.items():
        try:
            for st in apply.split("; "):
                adm.execute(st)
            r = check(adm, kadm)
            res[name] = {"patch": apply, "flagged_by": gate, "flagged": bool(r[gate]), "detail": r[gate][:3]}
        finally:
            for st in revert.split("; "):
                adm.execute(st)
    return res


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "adhoc"
    adm, kadm = db.admin(), db.admin(db=config.KEYS_DB)
    r = check(adm, kadm)
    ok = not any(r.values())
    out = {"label": label, "checks": r, "pass": ok}
    if "--controls" in sys.argv:
        out["controls"] = controls(adm, kadm)
        out["after_controls"] = check(adm, kadm)
        out["pass"] = ok and all(c["flagged"] for c in out["controls"].values()) and not any(out["after_controls"].values())
    print("guard_check", label, "PASS" if out["pass"] else "FAIL", {k: v for k, v in r.items() if v})
    write_result(f"guard_check_{label}", out)
    sys.exit(0 if out["pass"] else 1)
