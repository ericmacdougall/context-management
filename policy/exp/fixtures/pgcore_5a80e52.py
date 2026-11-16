"""Decision core on Postgres: the same reducer (pol._core.reduce) over a row-level view, made concurrency-safe by a fixed lock order.

Roles. dc_owner (NOLOGIN) owns every table, trigger and function. dc_app (LOGIN, SCRAM password read from a 0600 file; only the
SCRAM verifier, computed client-side, is ever sent to the server) is what the service connects as. dc_app may INSERT into dc_events
but holds no UPDATE, DELETE or TRUNCATE on it; it can only READ the authority tables (dc_principal, dc_role) and changes them only
through the SECURITY DEFINER function dc_apply_authority(seq), which applies an already logged revoke/grant event once, after
re-checking that its author is an unrevoked admin.

Append-only log. dc_events rejects UPDATE, DELETE (row triggers) and TRUNCATE (statement trigger), and dc_app lacks the privileges
anyway; disabling a trigger needs table ownership. A BEFORE INSERT trigger chains every event: chain_n and hash = sha256(prev_hash |
tenant | chain_n | code | ev::text) under the tenant's dc_chainhead row lock (FOR UPDATE), so the order is the chain order, not the
sequence order. dc_verify_chain(tenant) recomputes it inside the database.

Isolation and locks. Every transaction is READ COMMITTED (set explicitly). Lock order, always: tenant row (FOR SHARE; FOR UPDATE for
revoke and grant), a transaction advisory lock on (tenant, principal) for the idempotency key, the requirement's head row FOR UPDATE,
then the event insert (whose trigger locks the chain head). Every read the reducer makes happens after these locks, so a twin that
committed while this one waited is seen (`duplicate`, with the ORIGINAL outcome code, M2). The unique index on (tenant, principal,
idem) is a backstop; how often it fired is counted in BACKSTOP.

Proposals are rows (dc_props), counted open per requirement in the head row (open_n) and per author by an index, so the cost of an
event does not depend on how many proposals exist (N2). Values live in dc_values (the vault), never in events or heads (H7)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Iterable, List, Protocol

import psycopg
from psycopg import sql

from . import _core
from .labels import Label

HOST = "127.0.0.1"
BACKSTOP = [0]
RETRYABLE = (psycopg.errors.UniqueViolation, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)

DDL = r"""
drop schema if exists dc cascade;
create schema dc authorization dc_owner;
set role dc_owner;
create table dc.tenant(tenant text primary key, lk int not null default 0);
create table dc.principal(tenant text not null, pid text not null, kind text not null, admin bool not null, revoked bool not null default false,
                          primary key (tenant, pid));
create table dc.role(tenant text not null, req text not null, pid text not null, role text not null, primary key (tenant, req, pid, role));
create table dc.head(tenant text not null, req text not null, label jsonb not null, open_n int not null default 0, doc jsonb not null,
                     primary key (tenant, req));
create table dc.props(tenant text not null, req text not null, id text not null, kind text not null, author text not null, status text not null,
                      doc jsonb not null, primary key (tenant, req, id));
create index props_open_by_author on dc.props(tenant, req, author) where status = 'open';
create index props_open_author_all on dc.props(tenant, author) where status = 'open';
create table dc.chain(n bigserial primary key, tenant text not null, req text not null, doc jsonb not null);
create table dc.events(seq bigserial primary key, tenant text not null, principal text not null, idem text not null, req text, evid text not null,
                       fp text not null, code text not null, ev jsonb not null, chain_n bigint, prev_hash text, hash text);
create unique index events_idem on dc.events(tenant, principal, idem);
create unique index events_id on dc.events(tenant, req, evid) where req is not null;
create table dc.chainhead(tenant text primary key, n bigint not null default 0, hash text not null);
create table dc.auth_applied(seq bigint primary key);
create table dc.audit(n bigserial primary key, tenant text, principal text, code text not null, original text, ev jsonb);
create table dc.vals(tenant text not null, ref text not null, text text not null, label jsonb not null, subjects text[] not null,
                     primary key (tenant, ref));
create index vals_subjects on dc.vals using gin(subjects);
create table dc.subject_tokens(tenant text not null, subject text not null, token text not null unique, primary key (tenant, subject));

create function dc.events_chain() returns trigger language plpgsql security definer set search_path = dc, pg_temp as $$
declare h text; k bigint;
begin
  select hash, n + 1 into h, k from dc.chainhead where tenant = new.tenant for update;
  if h is null then raise exception 'no chain head for tenant %', new.tenant; end if;
  new.chain_n := k;
  new.prev_hash := h;
  new.hash := encode(sha256(convert_to(h || '|' || new.tenant || '|' || k::text || '|' || new.code || '|' || new.ev::text, 'UTF8')), 'hex');
  update dc.chainhead set hash = new.hash, n = k where tenant = new.tenant;
  return new;
end $$;
create trigger events_chain before insert on dc.events for each row execute function dc.events_chain();

create function dc.events_frozen() returns trigger language plpgsql as $$
begin raise exception 'dc.events is append-only (% refused)', tg_op; end $$;
create trigger events_no_update before update or delete on dc.events for each row execute function dc.events_frozen();
create trigger events_no_truncate before truncate on dc.events for each statement execute function dc.events_frozen();

create function dc.verify_chain(t text) returns table(n bigint, ok bool) language plpgsql security definer set search_path = dc, pg_temp as $$
declare h text := repeat('0', 64); r record; good bool := true; cnt bigint := 0;
begin
  for r in select * from dc.events e where e.tenant = t order by e.chain_n loop
    cnt := cnt + 1;
    if r.chain_n <> cnt or r.prev_hash <> h or r.hash <> encode(sha256(convert_to(h || '|' || r.tenant || '|' || r.chain_n::text || '|' || r.code || '|' || r.ev::text, 'UTF8')), 'hex') then
      good := false;
    end if;
    h := r.hash;
  end loop;
  if h <> (select c.hash from dc.chainhead c where c.tenant = t) then good := false; end if;
  return query select cnt, good;
end $$;

create function dc.apply_authority(s bigint) returns text language plpgsql security definer set search_path = dc, pg_temp as $$
declare e record; tgt text; n int;
begin
  select * into e from dc.events where seq = s;
  if e is null or e.code not in ('revoked', 'granted') then raise exception 'not an authority event'; end if;
  if exists (select 1 from dc.auth_applied where seq = s) then raise exception 'already applied'; end if;
  if not exists (select 1 from dc.principal p where p.tenant = e.tenant and p.pid = e.principal and p.admin and not p.revoked) then
    raise exception 'author is not an unrevoked admin';
  end if;
  perform 1 from dc.tenant where tenant = e.tenant for update;
  tgt := e.ev->>'target';
  if e.code = 'revoked' then
    update dc.principal set revoked = true where tenant = e.tenant and pid = tgt;
    for r_req, n in select req, count(*) from dc.props where tenant = e.tenant and author = tgt and status = 'open' group by req loop
      null;
    end loop;
  else
    insert into dc.role values (e.tenant, e.ev->>'req', tgt, e.ev->>'role') on conflict do nothing;
  end if;
  insert into dc.auth_applied values (s);
  return e.code;
end $$;
reset role;
"""

# plpgsql FOR with two loop targets needs declared vars; the revoke branch is written out explicitly here (replaces the stub loop above).
DDL = DDL.replace("""    for r_req, n in select req, count(*) from dc.props where tenant = e.tenant and author = tgt and status = 'open' group by req loop
      null;
    end loop;""", """    update dc.head h set open_n = h.open_n - x.c
      from (select req, count(*) c from dc.props where tenant = e.tenant and author = tgt and status = 'open' group by req) x
      where h.tenant = e.tenant and h.req = x.req;
    update dc.props set status = 'invalidated', doc = jsonb_set(jsonb_set(doc, '{status}', '"invalidated"'), '{closed_by}', 'null')
      where tenant = e.tenant and author = tgt and status = 'open';""")

GRANTS = """
grant usage on schema dc to dc_app;
grant select, update(lk) on dc.tenant to dc_app;
grant select on dc.principal, dc.role, dc.chainhead, dc.auth_applied to dc_app;
grant select, insert, update on dc.head, dc.props to dc_app;
grant select, insert on dc.chain, dc.events, dc.audit to dc_app;
grant select, insert, delete on dc.vals to dc_app;
grant select, insert, delete on dc.subject_tokens to dc_app;
grant usage on all sequences in schema dc to dc_app;
grant execute on function dc.apply_authority(bigint), dc.verify_chain(text) to dc_app;
revoke update, delete, truncate on dc.events from dc_app;
"""


def _scram_verifier(password: str) -> str:
    salt, it = secrets.token_bytes(16), 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, it)
    ck = hmac.new(salted, b"Client Key", "sha256").digest()
    sk = hmac.new(salted, b"Server Key", "sha256").digest()
    b = lambda x: base64.b64encode(x).decode()          # noqa: E731
    return "SCRAM-SHA-256$%d:%s$%s:%s" % (it, b(salt), b(hashlib.sha256(ck).digest()), b(sk))


def _dbname() -> str:
    """The database name: POL_PG_DB (a name, never a secret), default "pol". Every connect call goes through here."""
    return os.environ.get("POL_PG_DB") or "pol"


def _connect(user, passfile, port, app):
    c = psycopg.connect(host=HOST, port=port, dbname=_dbname(), user=user, passfile=passfile, autocommit=True, application_name=app)
    c.isolation_level = psycopg.IsolationLevel.READ_COMMITTED            # explicit; the lock protocol is argued for READ COMMITTED
    return c


def connect_app(port: int, passfile: str, app: str = "pol"):
    """A dc_app connection (password from the 0600 passfile; never from the environment or a command line)."""
    return _connect("dc_app", passfile, port, app)


def install(port: int, super_passfile: str, app_password_file: str, tenants):
    """Admin path (run once, as the database superuser): roles, schema, grants and each tenant's genesis.
    tenants: {tenant: config} with config as for pol._core.MemoryState. The dc_app password is read from its file and only its SCRAM
    verifier is sent."""
    if (os.stat(app_password_file).st_mode & 0o077) or (os.stat(super_passfile).st_mode & 0o077):
        raise PermissionError("password files must be mode 0600")
    with open(app_password_file, encoding="utf-8") as f:
        pw = f.read().strip()
    c = _connect("postgres", super_passfile, port, "pol-install")
    try:
        with c.transaction():
            for r, login in (("dc_owner", False), ("dc_app", True)):
                if c.execute("select 1 from pg_roles where rolname=%s", (r,)).fetchone() is None:
                    c.execute(sql.SQL("create role {} " + ("login" if login else "nologin")).format(sql.Identifier(r)))
            c.execute(sql.SQL("alter role dc_app with login password {}").format(sql.Literal(_scram_verifier(pw))))
            c.execute("grant dc_owner to postgres")
            c.execute(DDL)
            c.execute(GRANTS)
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
                    c.execute("insert into dc.head(tenant, req, label, doc) values (%s,%s,%s,%s)",
                              (t, req, json.dumps(st.req_labels[req]), json.dumps(st.heads[req])))
    finally:
        c.close()
    del pw


class _View:
    """pol._core's read port over rows, inside the caller's transaction (locks already held)."""

    def __init__(self, conn, tenant):
        self.c, self.tenant = conn, tenant
        self._p, self._heads = {}, {}

    def _prin(self, pid):
        if pid not in self._p:
            self._p[pid] = self.c.execute("select kind, admin, revoked from dc.principal where tenant=%s and pid=%s", (self.tenant, pid)).fetchone() \
                if isinstance(pid, str) else None
        return self._p[pid]

    def kind(self, pid):
        r = self._prin(pid)
        return None if r is None else r[0]

    def is_admin(self, pid):
        r = self._prin(pid)
        return bool(r and r[1])

    def is_revoked(self, pid):
        r = self._prin(pid)
        return bool(r and r[2])

    def _role(self, req, pid, role):
        return self.c.execute("select 1 from dc.role where tenant=%s and req=%s and pid=%s and role=%s", (self.tenant, req, pid, role)).fetchone() is not None

    def has_authority(self, req, pid):
        return self._role(req, pid, "decide")

    def is_proposer(self, req, pid):
        return self._role(req, pid, "propose")

    def _head(self, req):
        if req not in self._heads:
            self._heads[req] = self.c.execute("select label, open_n, doc from dc.head where tenant=%s and req=%s", (self.tenant, req)).fetchone()
        return self._heads[req]

    def req_exists(self, req):
        return self._head(req) is not None

    def req_label(self, req):
        return self._head(req)[0]

    def head(self, req):
        return self._head(req)[2]

    def prop(self, req, iid):
        r = self.c.execute("select doc from dc.props where tenant=%s and req=%s and id=%s", (self.tenant, req, iid)).fetchone()
        return None if r is None else r[0]

    def id_used(self, req, iid):
        return self.c.execute("select 1 from dc.events where tenant=%s and req=%s and evid=%s", (self.tenant, req, iid)).fetchone() is not None

    def open_count(self, req, pid):
        return self.c.execute("select count(*) from dc.props where tenant=%s and req=%s and author=%s and status='open'", (self.tenant, req, pid)).fetchone()[0]

    def open_nonhuman(self, req):
        # open items with a non-human author (R3-4); bounded by MAX_OPEN_PER_REQ plus 16 per human, under the head lock
        return self.c.execute("select count(*) from dc.props where tenant=%s and req=%s and status='open' and doc->>'author_kind' <> 'human'",
                              (self.tenant, req)).fetchone()[0]

    def idem(self, pid, key):
        r = self.c.execute("select fp, code from dc.events where tenant=%s and principal=%s and idem=%s", (self.tenant, pid, key)).fetchone()
        return None if r is None else [r[0], r[1]]


def _apply(c, tenant, ev, code, delta):
    """Write the delta and the event row; returns the event's seq."""
    req = ev["req"] if ev["type"] != "revoke" else None
    for op in delta:
        k = op[0]
        if k == "head":
            c.execute("update dc.head set doc=%s where tenant=%s and req=%s", (json.dumps(op[2]), tenant, op[1]))
        elif k == "prop_new":
            d = op[2]
            c.execute("insert into dc.props values (%s,%s,%s,%s,%s,'open',%s)", (tenant, op[1], d["id"], d["kind"], d["author"], json.dumps(d)))
            c.execute("update dc.head set open_n = open_n + 1 where tenant=%s and req=%s", (tenant, op[1]))
        elif k == "prop_close":
            _, r, iid, status, by = op
            was = c.execute("select status from dc.props where tenant=%s and req=%s and id=%s", (tenant, r, iid)).fetchone()[0]
            c.execute("update dc.props set status=%s, doc = jsonb_set(jsonb_set(doc, '{status}', to_jsonb(%s::text)), '{closed_by}', to_jsonb(%s::text)) "
                      "where tenant=%s and req=%s and id=%s", (status, status, by, tenant, r, iid))
            if was == "open":
                c.execute("update dc.head set open_n = open_n - 1 where tenant=%s and req=%s", (tenant, r))
        elif k == "chain":
            c.execute("insert into dc.chain(tenant, req, doc) values (%s,%s,%s)", (tenant, op[1], json.dumps(op[2])))
        elif k in ("idem", "id", "revoke", "grant"):
            pass                       # carried by the event row / applied by dc.apply_authority after it
        else:
            raise AssertionError(k)
    seq = c.execute("insert into dc.events(tenant, principal, idem, req, evid, fp, code, ev) values (%s,%s,%s,%s,%s,%s,%s,%s) returning seq",
                    (tenant, ev["principal"], ev["idem"], req, ev["id"], _core.sha(ev), code, json.dumps(ev))).fetchone()[0]
    if code in ("revoked", "granted"):
        c.execute("select dc.apply_authority(%s)", (seq,))
    return seq


class PgEngine:
    """One tenant's decision core in Postgres over a dc_app connection. Not thread-safe: one engine per connection."""

    def __init__(self, conn, tenant: str):
        self._c, self.tenant = conn, tenant

    def _submit(self, ev, hook=None):
        """(code, original_code). hook(name) is a test seam called inside the transaction ('locked', 'before_commit')."""
        for attempt in range(8):
            try:
                with self._c.transaction():
                    code, orig = self._once(ev, hook)
                return code, orig
            except RETRYABLE as e:
                if isinstance(e, psycopg.errors.UniqueViolation):
                    BACKSTOP[0] += 1
                if attempt == 7:
                    raise
        raise RuntimeError("unreachable")

    def _once(self, ev, hook):
        c, t = self._c, self.tenant
        ok = isinstance(ev, dict)
        typ = ev.get("type") if ok else None
        row = c.execute("select tenant from dc.tenant where tenant=%s " + ("for update" if typ in ("revoke", "grant") else "for share"), (t,)).fetchone()
        if row is None:
            raise LookupError("tenant not installed")
        if ok and isinstance(ev.get("principal"), str):
            c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (json.dumps([t, ev["principal"]]),))
        if ok and typ != "revoke" and isinstance(ev.get("req"), str):
            c.execute("select 1 from dc.head where tenant=%s and req=%s for update", (t, ev["req"]))
        if hook:
            hook("locked")
        code, orig, delta = _core.reduce(_View(c, t), ev)
        if code in _core.REFUSED:
            try:
                evj = json.dumps(ev)
            except (TypeError, ValueError):
                evj = json.dumps({"unserialisable": True})
            c.execute("insert into dc.audit(tenant, principal, code, original, ev) values (%s,%s,%s,%s,%s)",
                      (t, ev.get("principal") if ok and isinstance(ev.get("principal"), str) else None, code, orig, evj))
        else:
            _apply(c, t, ev, code, delta)
        if hook:
            hook("before_commit")
        return code, orig

    # ---- private reads for the service and the experiments
    def _revoked(self):
        return [r[0] for r in self._c.execute("select pid from dc.principal where tenant=%s and revoked", (self.tenant,)).fetchall()]

    def _participant(self, pid, req):
        c = self._c
        return c.execute("select 1 from dc.head where tenant=%s and req=%s", (self.tenant, req)).fetchone() is not None and (
            c.execute("select 1 from dc.role where tenant=%s and req=%s and pid=%s", (self.tenant, req, pid)).fetchone() is not None
            or self._is_admin(pid))

    def _is_admin(self, pid):
        r = self._c.execute("select admin from dc.principal where tenant=%s and pid=%s", (self.tenant, pid)).fetchone()
        return bool(r and r[0])

    def _reqs(self):
        return [r for (r,) in self._c.execute("select req from dc.head where tenant=%s order by req", (self.tenant,)).fetchall()]

    def _head(self, req):
        r = self._c.execute("select doc from dc.head where tenant=%s and req=%s", (self.tenant, req)).fetchone()
        return None if r is None else r[0]

    def _open_items(self, req):
        return [d for (d,) in self._c.execute("select doc from dc.props where tenant=%s and req=%s and status='open'", (self.tenant, req)).fetchall()]

    def _comparable(self):
        """CORE-RULES section 8 view, built from the rows (B3 compares it with the in-memory engine)."""
        c, t = self._c, self.tenant
        prin = c.execute("select pid, revoked from dc.principal where tenant=%s", (t,)).fetchall()
        roles = c.execute("select req, pid, role from dc.role where tenant=%s", (t,)).fetchall()
        heads = dict(c.execute("select req, doc from dc.head where tenant=%s", (t,)).fetchall())
        props = c.execute("select req, id, kind, status from dc.props where tenant=%s", (t,)).fetchall()
        out = {"revoked": sorted(p for p, r in prin if r), "authority": {r: [] for r in heads}, "proposers": {r: [] for r in heads}, "reqs": {},
               "proposals": {r: {} for r in heads}, "pending": {r: {} for r in heads}}
        for req, pid, role in roles:
            out["authority" if role == "decide" else "proposers"][req].append(pid)
        for k in ("authority", "proposers"):
            out[k] = {r: sorted(v) for r, v in out[k].items()}
        for r, h in heads.items():
            out["reqs"][r] = {"version": h["version"], "status": h["status"], "value_ref": h["value_ref"], "value_event": h["value_event"],
                              "value_by": h["value_by"], "value_version": h["value_version"], "value_label": h["value_label"],
                              "contested": None if h["contested"] is None else {"base": h["contested"]["base"], "candidates": [
                                  [x["event"], x["by"], x["effect"], x["verb"]] for x in h["contested"]["candidates"]]}}
        for req, iid, kind, status in props:
            out["proposals" if kind == "proposal" else "pending"][req][iid] = status
        return out

    def _verify_chain(self):
        return self._c.execute("select n, ok from dc.verify_chain(%s)", (self.tenant,)).fetchone()


class PgVault:
    """dc.vals: referenced values with subject tags (refs assigned here)."""

    def __init__(self, conn, key_file: str):
        """key_file: a 0600 file holding the vault's HMAC key (hex); the key is never stored in the database."""
        if os.stat(key_file).st_mode & 0o077:
            raise PermissionError("vault key file must be mode 0600")
        with open(key_file, encoding="utf-8") as f:
            self._key = bytes.fromhex(f.read().strip())
        self._c = conn

    def _ref(self, tenant, principal, idem, text, label: Label):
        from .decisions import _value_ref
        return _value_ref(self._key, tenant, principal, idem, text, label)

    def _event_label(self, tenant, label: Label):
        """Random token per (tenant, canonical subject) in dc.subject_tokens, minted on first use; erasure deletes the row (R3-3)."""
        from .decisions import _tokenised
        toks = []
        for x in sorted(label.subjects):
            self._c.execute("insert into dc.subject_tokens values (%s,%s,%s) on conflict do nothing", (tenant, x, "subj-" + secrets.token_hex(16)))
            toks.append(self._c.execute("select token from dc.subject_tokens where tenant=%s and subject=%s", (tenant, x)).fetchone()[0])
        return _tokenised(label, toks)

    def _forget_subject(self, tenant, subject):
        self._c.execute("delete from dc.subject_tokens where tenant=%s and subject=%s", (tenant, subject))

    def _put(self, tenant, ref, text, label: Label):
        return self._c.execute("insert into dc.vals values (%s,%s,%s,%s,%s) on conflict do nothing",
                               (tenant, ref, text, json.dumps(label.to_dict()), sorted(label.subjects))).rowcount == 1

    def _get(self, tenant, ref):
        r = self._c.execute("select text, label from dc.vals where tenant=%s and ref=%s", (tenant, ref)).fetchone() if isinstance(ref, str) else None
        return None if r is None else {"tenant": tenant, "text": r[0], "label": Label.from_dict(r[1])}

    def _delete(self, tenant, ref):
        return self._c.execute("delete from dc.vals where tenant=%s and ref=%s", (tenant, ref)).rowcount > 0

    def _refs_about(self, tenant, subject):
        return [r for (r,) in self._c.execute("select ref from dc.vals where tenant=%s and %s = any(subjects) order by ref", (tenant, subject)).fetchall()]


# ------------------------------------------------------------------------------------------------ R3-8: external anchor
class Anchor(Protocol):
    """Where chain heads are recorded OUTSIDE the database. A WORM bucket, an object store with retention or a transparency log can replace
    FileAnchor; the contract is append-only records and reading them back in order."""

    def append(self, rec: dict) -> None: ...

    def records(self) -> Iterable[dict]: ...


class FileAnchor:
    """Append-only JSONL file outside the database: one {"tenant", "n", "hash", "utc"} per line. O_APPEND, fsync per record; the class has no
    method that rewrites or removes a record. (A file on the same host is only as strong as that host's file permissions: for a real
    guarantee point the path at storage the database owner cannot write, or implement Anchor over a WORM store.)"""

    def __init__(self, path: str):
        self.path = path

    def append(self, rec: dict) -> None:
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def records(self) -> List[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []


def anchor_export(conn, anchor: Anchor, tenant: str) -> dict:
    """Append the tenant's current chain head (n, hash) to the anchor. Runs on an owner/operator connection, never dc_app (which cannot
    read the chain). Returns the record."""
    r = conn.execute("select n, hash from dc.chainhead where tenant = %s", (tenant,)).fetchone()
    if r is None:
        raise LookupError("tenant not installed")
    rec = {"tenant": tenant, "n": int(r[0]), "hash": r[1], "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    anchor.append(rec)
    return rec


def anchor_check(conn, anchor: Anchor, tenant: str) -> dict:
    """True only when the chain verifies inside the database AND every anchored (n, hash) of this tenant is still the event at chain_n = n.
    Detects an owner who rewrote history and recomputed the hashes for any event up to the LAST export: the recomputed hash of an anchored
    event differs from the anchored one as soon as any earlier byte changed. It does NOT detect a rewrite of events after the last export
    (nothing outside the database has seen them yet): export often."""
    recs = [x for x in anchor.records() if x.get("tenant") == tenant]
    n_ok, chain_ok = conn.execute("select n, ok from dc.verify_chain(%s)", (tenant,)).fetchone()
    bad = []
    for x in recs:
        row = conn.execute("select hash from dc.events where tenant = %s and chain_n = %s", (tenant, x["n"])).fetchone()
        if row is None:
            bad.append({"n": x["n"], "problem": "missing", "anchored": x["hash"]})
        elif row[0] != x["hash"]:
            bad.append({"n": x["n"], "problem": "hash_differs", "anchored": x["hash"], "found": row[0]})
    return {"ok": bool(chain_ok) and not bad, "chain_ok": bool(chain_ok), "events": int(n_ok), "anchored": len(recs), "mismatches": bad}
