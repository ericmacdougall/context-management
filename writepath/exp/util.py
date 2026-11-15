"""Shared experiment helpers: fresh tenants, event builders, ES counts."""
import datetime as dt
import json
import uuid

from wp import db, families as F, keys as K, lease as L, log
from wp.es import ES

TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def tenant(prefix):
    return f"{prefix}{uuid.uuid4().hex[:6]}"


def env(dbname=None):
    conn = db.connect(db=dbname) if dbname else db.connect()
    return conn, K.KeyService(), ES()


def msg(i, text, subj, kind="message", **kw):
    return dict(subject_id=subj, kind=kind, actor="user", trust_origin="user_typed", ts_event=TS + dt.timedelta(seconds=i), content=text, **kw)


def fact(i, subj, entity, attribute, value):
    return msg(i, json.dumps({"entity": entity, "attribute": attribute, "value": value}), subj, kind="fact")


def session(conn, t, sid="s1", ttl=30.0, force=False):
    return L.acquire(conn, t, L.session_scope(sid), ttl, force=force)


def es_count_all(es, t, q):
    """Count over EVERY index of the tenant (aliased or orphaned)."""
    es.req("POST", f"/{F.P}{t}-*/_refresh", ok=(200, 404), expand_wildcards="all", ignore_unavailable=True, allow_no_indices=True)
    return es.count(f"{F.P}{t}-*", q)


def es_count_live(es, t, family, q):
    es.req("POST", f"/{F.read_alias(t, family)}/_refresh", ok=(200, 404))
    return es.count(F.read_alias(t, family), q)


def drop_tenant(es, t):
    for i in es.indices(f"{F.P}{t}-*"):
        es.req("DELETE", f"/{i}", ok=(200, 404))
