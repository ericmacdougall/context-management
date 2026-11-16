"""Controls as ONE named patch to the live store (review: a control must differ from the fenced arm only in the thing
under test; no all-triggers-dropped database). Each patch is SQL applied by the admin and reverted in `finally`;
the patch text goes into the result. Function variants are cut from wp/schema.sql, so a control is exactly the
shipped function with one check removed. guard_check runs at the start and end of every proof run, so a patch that
fails to revert fails the run."""
import contextlib
import pathlib

from wp import config, db

SCHEMA = (pathlib.Path(__file__).resolve().parent.parent / "wp" / "schema.sql").read_text()


def function_sql(name):
    start = SCHEMA.index(f"create function {name}(")
    first = SCHEMA.index("$$", start)
    end = SCHEMA.index("$$;", first + 2) + 3
    return SCHEMA[start:end]


def variant(name, old, new=None):
    """(apply, revert) SQL replacing one fragment (or a list of (old, new) pairs) of a shipped function."""
    orig = function_sql(name)
    mod = orig
    for o, n in ([(old, new)] if new is not None else old):
        assert o in mod, f"{name}: fragment not found"
        mod = mod.replace(o, n, 1)
    return ([f"set role wp_owner", mod.replace("create function", "create or replace function", 1), "reset role"],
            ["set role wp_owner", orig.replace("create function", "create or replace function", 1), "reset role"])


@contextlib.contextmanager
def patched(apply, revert, keys_db=False):
    adm = db.admin(db=config.KEYS_DB if keys_db else None)
    try:
        for s in apply:
            s(adm) if callable(s) else adm.execute(s)
        yield
    finally:
        for s in revert:
            s(adm) if callable(s) else adm.execute(s)
        adm.close()


def drop_duplicate_decisions(adm):
    """Revert step for the single-use-source control: the control arm wrote a second decision on one source, which the
    restored unique index would reject. Remove those control rows (admin, append-only trigger off for this statement only)."""
    adm.execute("alter table events disable trigger events_no_change")
    try:
        adm.execute("delete from events e using events f where e.kind = 'decision' and f.kind = 'decision' and e.tenant_id = f.tenant_id "
                    "and e.source_event_id = f.source_event_id and e.ingest_seq > f.ingest_seq")
    finally:
        adm.execute("alter table events enable always trigger events_no_change")


NOOP = ([], [])
