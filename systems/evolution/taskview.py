"""Postgres plumbing for tasks: append task events to the encrypted log, materialise `task_state` (rebuildable),
and a task-level concept fold fenced on the view version."""
import json, os, signal
from memsys import store, db as _db
from . import task

DDL = """
create table if not exists task_view (tenant_id text, session_id text, state jsonb not null, applied_seq bigint not null default 0,
  primary key (tenant_id, session_id));
create table if not exists task_concepts (tenant_id text, session_id text, built_seq bigint not null, def_version int not null, digest text not null,
  committed_at timestamptz not null default clock_timestamp(), primary key (tenant_id, session_id));
"""
import datetime as dt
TS0 = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def setup(conn):
    conn.execute(DDL)


def kill(name, at):
    if name == at:
        os.kill(os.getpid(), signal.SIGKILL)


def append(conn, tenant, session, kind, body, trust="user_typed", n=0):
    ids, _ = store.append_events(conn, tenant, session, [dict(ts_event=TS0 + dt.timedelta(seconds=n), actor="user" if trust == "user_typed" else trust,
                                                              trust_origin=trust, kind=kind, subject_id="u1", content=json.dumps(body, sort_keys=True))])
    return int(ids[0].rsplit("/", 1)[1])


def append_many(conn, tenant, session, events):
    evs = [dict(ts_event=TS0 + dt.timedelta(seconds=e["seq"]), actor=e["trust_origin"], trust_origin=e["trust_origin"], kind=e["kind"], subject_id="u1",
                content=json.dumps(e["body"], sort_keys=True)) for e in events]
    for i in range(0, len(evs), 100):
        store.append_events(conn, tenant, session, evs[i:i + 100])


def load_events(conn, tenant, session, after_seq=0):
    rows = conn.execute("select seq, kind, trust_origin, mem_dec(tenant_id, subject_id, content_enc) from events where tenant_id=%s and session_id=%s and seq>%s order by seq",
                        (tenant, session, after_seq)).fetchall()
    return [{"seq": s, "kind": k, "trust_origin": t, "body": json.loads(c)} for s, k, t, c in rows if c is not None]


def update_view(conn, tenant, session, crash_at=None):
    """Idempotent catch-up: apply every event above applied_seq to the stored state and commit state+cursor together."""
    n = 0
    with conn.transaction():
        conn.execute("insert into task_view(tenant_id, session_id, state) values (%s,%s,%s) on conflict do nothing", (tenant, session, json.dumps(task.initial())))
        st, applied = conn.execute("select state, applied_seq from task_view where tenant_id=%s and session_id=%s for update", (tenant, session)).fetchone()
        for ev in load_events(conn, tenant, session, applied):
            st = task.apply(st, ev, inplace=True); n += 1
        kill("mid_update", crash_at)
        conn.execute("update task_view set state=%s, applied_seq=%s where tenant_id=%s and session_id=%s",
                     (json.dumps(st, sort_keys=True), st["applied"], tenant, session))
    kill("after_update", crash_at)
    return n


def read_view(conn, tenant, session):
    r = conn.execute("select state, applied_seq from task_view where tenant_id=%s and session_id=%s", (tenant, session)).fetchone()
    return r


def concept_digest(state):
    v = task.view(state)
    import hashlib
    return hashlib.sha256(task.canon({"def": v["items"], "classes": [(r["step_id"], r["class_now"]) for r in v["steps"]]}).encode()).hexdigest()[:16]
