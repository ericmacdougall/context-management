"""Create (or reset, lab only) the schemas. usage: python -m wp.setup [--reset]
Also creates database wp_ctl: the SAME schema with the fence and guard triggers dropped, used only as the broken-store
control in experiments (it is never used by the write path)."""
import pathlib
import sys

from . import config, db, registry

HERE = pathlib.Path(__file__).parent
CTL_DB = "wp_ctl"
CTL_DROP = ["drop trigger events_guard on events", "drop trigger inbox_fence on inbox", "drop trigger jobs_guard on analyst_jobs",
            "drop trigger facts_fence on facts", "drop trigger cursors_fence on cursors", "drop trigger erasure_guard on erasure_jobs",
            "drop trigger heads_guard on decision_heads", "drop trigger inbox_subject on inbox", "drop trigger facts_subject on facts",
            "drop trigger quarantine_subject on quarantine", "alter table analyst_jobs drop constraint done_needs_verify",
            "alter table events drop constraint events_tenant_id_idem_key_key"]


def reset_db(conn, sql):
    conn.execute("drop schema public cascade")
    conn.execute("create schema public")
    conn.execute(sql)


def main(reset):
    assert config.PG_PORT == 25460
    admin = db.connect(db="postgres")
    have = {r[0] for r in admin.execute("select datname from pg_database").fetchall()}
    for d in (config.EVENTS_DB, config.KEYS_DB, CTL_DB):
        if d not in have:
            admin.execute(f'create database "{d}"')
    ev, ks, ctl = db.connect(), db.keys_connect(), db.connect(db=CTL_DB)
    if reset:
        reset_db(ev, (HERE / "schema.sql").read_text())
        reset_db(ks, (HERE / "keys_schema.sql").read_text())
        reset_db(ctl, (HERE / "schema.sql").read_text())
        for s in CTL_DROP:
            ctl.execute(s)
    registry.sync_db(ev)
    registry.sync_db(ctl)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("setup ok", "reset" if reset else "")


if __name__ == "__main__":
    main("--reset" in sys.argv)
