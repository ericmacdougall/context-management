"""Decision core on Postgres, enforced by the database (round 3, R3-7). The DDL, the plpgsql core and the API are in pol/_pgcore.sql.

Roles. dc_owner (NOLOGIN) owns every table, trigger and function. dc_app (LOGIN) is the request handler's role: it holds NO table,
column or sequence privilege and may only EXECUTE the session-keyed API (dc.submit, dc.explain, dc.read_table, dc.erase_candidates,
dc.erase_values; SECURITY DEFINER, search_path pinned). dc_idp (LOGIN) is the identity adapter's role: it may only EXECUTE
dc.open_session(proof, ttl) and dc.end_session(token). A session is opened only from a PROOF made by an enrolled factor (HMAC under a
key that only owner code can read, single use, short-lived); tenant, principal and channel come from the factor row, so neither role
can mint a session from a bare principal id (step 2, R3-1). Factors are enrolled on the admin path (dc.enroll_factor, owner only).
Passwords are read from 0600 files; only SCRAM verifiers computed client-side are sent to the server.

The decision rules run in plpgsql (dc._rules, ported from pol/_core.py, which stays as the differential oracle). Identity comes only
from the sessions row; authority changes are applied inside the same transaction from the event the function built and judged; heads,
proposals, values and subject tokens are written only by owner code. dc._apply_event (raw events, no session) is OWNER-ONLY, for the
differential and concurrency experiments; no login role can execute it.

Trust split (disclosed). The database enforces who may change decision state and who may read raw state (a verified session of a
participant). Label-level filtering of reads and the legal-hold check of erasure stay in the in-process policy point (pol.enforce):
that part is trusted code, not enforced by the database. Python-private names are not enforcement either (tests/test_surface.py).

Locks (S1, FINAL-JUDGMENT.md section 4): every call takes all its locks before it judges, in one global order (relation locks, the
authority lock, the authority rows FOR SHARE, the tenant lock, the idempotency lock, data rows, subject locks, the chain head, the
nonce lock), each wait capped by the credential's remaining life; then the writes, the final judgment and a revoke's authority writes
run under lock_timeout 1 ms. READ COMMITTED, set explicitly. install() sets idle_in_transaction_session_timeout = 60 s as the role
default of dc_app and dc_idp, which bounds how long a caller-held transaction can delay a revocation.

ASSUMPTION (round 3, not confirmed by the user): non-humans only propose and remark; the per-req cap binds non-humans; idempotency is
per (tenant, principal, idem) with a fingerprint over the event, never the session token."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Protocol

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import _core
from .labels import Label, LabelError, canon_subject

HOST = "127.0.0.1"
BACKSTOP = [0]
RETRYABLE = (psycopg.errors.UniqueViolation, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)
_SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pgcore.sql")


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


def connect_idp(port: int, passfile: str, app: str = "pol-idp"):
    """A dc_idp connection, for the identity adapter's side only (never handed to a request handler)."""
    return _connect("dc_idp", passfile, port, app)


def _read_secret(path):
    if os.stat(path).st_mode & 0o077:
        raise PermissionError("password files must be mode 0600")
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def install(port: int, super_passfile: str, app_password_file: str, tenants, idp_password_file: Optional[str] = None):
    """Admin path (run as the database superuser): roles, schema, core, grants and each tenant's genesis.
    tenants: {tenant: config} as for pol._core.MemoryState. Only SCRAM verifiers of the role passwords are sent."""
    if os.stat(super_passfile).st_mode & 0o077:
        raise PermissionError("password files must be mode 0600")
    pws = {"dc_app": _read_secret(app_password_file)}
    if idp_password_file:
        pws["dc_idp"] = _read_secret(idp_password_file)
    with open(_SQL_PATH, encoding="utf-8") as f:
        ddl = f.read()
    c = _connect("postgres", super_passfile, port, "pol-install")
    try:
        with c.transaction():
            for r, login in (("dc_owner", False), ("dc_app", True), ("dc_idp", True)):
                if c.execute("select 1 from pg_roles where rolname=%s", (r,)).fetchone() is None:
                    c.execute(sql.SQL("create role {} " + ("login" if login else "nologin")).format(sql.Identifier(r)))
            for r, pw in pws.items():
                c.execute(sql.SQL("alter role {} with login password {}").format(sql.Identifier(r), sql.Literal(_scram_verifier(pw))))
            for r in ("dc_app", "dc_idp"):           # S1: a holder of pinned authority cannot sit idle in a transaction for long
                c.execute(sql.SQL("alter role {} set idle_in_transaction_session_timeout = '60s'").format(sql.Identifier(r)))
            c.execute("grant dc_owner to postgres")
            c.execute(ddl)
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
                              (t, req, Jsonb(st.req_labels[req]), Jsonb(st.heads[req])))
    finally:
        c.close()
    pws.clear()


class PgEngine:
    """RAW core over an OWNER connection (superuser or dc_owner): tests and experiments only (differential, B3). Executes the owner-only
    dc._lock_event and dc._reduce_apply; a dc_app connection cannot (privileges), which tests/test_pg_boundary.py proves."""

    def __init__(self, conn, tenant: str):
        self._c, self.tenant = conn, tenant

    def _submit(self, ev, hook=None):
        """(code, original_code). hook(name) is a test seam called inside the transaction ('locked', 'before_commit')."""
        for attempt in range(8):
            try:
                with self._c.transaction():
                    self._c.execute("select dc._lock_event(%s, %s)", (self.tenant, Jsonb(ev)))
                    if hook:
                        hook("locked")
                    r = self._c.execute("select dc._reduce_apply(%s, %s)", (self.tenant, Jsonb(ev))).fetchone()[0]
                    if hook:
                        hook("before_commit")
                return r["code"], r["original"]
            except RETRYABLE as e:
                if isinstance(e, psycopg.errors.UniqueViolation):
                    BACKSTOP[0] += 1
                if attempt == 7:
                    raise
            except (TypeError, ValueError):
                return self._submit({"unserialisable": True}, hook)
        raise RuntimeError("unreachable")

    def _revoked(self):
        return [r[0] for r in self._c.execute("select pid from dc.principal where tenant=%s and revoked", (self.tenant,)).fetchall()]

    def _head(self, req):
        r = self._c.execute("select doc from dc.head where tenant=%s and req=%s", (self.tenant, req)).fetchone()
        return None if r is None else r[0]

    def _comparable(self):
        """CORE-RULES section 8 view, built from the rows (compared with pol._core.MemoryState.comparable)."""
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


# ------------------------------------------------------------------------------------------------ the deployment path (dc_idp / dc_app)
PG_TOKEN = "pg_token"          # name of the database session token attached to the in-process Session (SessionAuthority._bind)


@dataclass(frozen=True)
class PgSession:
    """An in-process Session (pol.directory) and the database session token the bridge opened FOR IT. The pair is accepted only when the
    token equals the one the SessionAuthority holds for that exact live Session (attached at issue), so a hand-built pair mixing two
    principals, two channels or two tenants is refused before any database call (review-3 N1)."""
    session: object
    db_token: str


class PgIdentity:
    """The identity adapter (pol.identity.IdentityAdapter) for the Postgres deployment: a credential is a factor's PROOF, and the
    database verifies it (dc.open_session over the dc_idp connection, which opens the session in the same call). The opened session's
    token and stamped identity are kept for the bridge in a per-thread slot, because SessionAuthority.issue calls authenticate on the
    caller's thread and returns only the in-process Session."""

    def __init__(self, idp_conn, ttl_s: int = 3600):
        if idp_conn is None:
            raise TypeError("PgIdentity(idp_conn)")
        self._c, self._ttl, self._slot = idp_conn, int(ttl_s), threading.local()

    def authenticate(self, credential):
        from .identity import Assertion
        self._slot.opened = None
        d = self._c.execute("select dc.open_session(%s, %s)", (credential, self._ttl)).fetchone()[0]
        self._slot.opened = d
        return Assertion(d["principal"], d["channel"], d["factor"])

    def _take(self):
        d, self._slot.opened = getattr(self._slot, "opened", None), None
        return d

    def _end(self, token) -> bool:
        return bool(self._c.execute("select dc.end_session(%s)", (token,)).fetchone()[0])


class PgSessionBridge:
    """Issues a PgSession from a proof: the SessionAuthority (whose identity adapter is a PgIdentity) verifies it through the database,
    which opens the database session; the in-process Session must name the same tenant, principal and channel, and its token is attached
    to that Session (review-3 N1). Any failure after the database session opened ends it, so no usable token is left behind. end() ends
    both sides. A request handler holds a PgDecisionService (dc_app) only; this object holds the dc_idp side."""

    def __init__(self, sessions):
        idp = getattr(sessions, "_idp", None)
        if not isinstance(idp, PgIdentity):
            raise TypeError("PgSessionBridge needs a SessionAuthority whose identity adapter is a PgIdentity")
        self._sessions, self._idp = sessions, idp

    def issue(self, proof) -> PgSession:
        from .identity import AuthenticationFailed
        try:
            s = self._sessions.issue(proof)
        except BaseException:
            d = self._idp._take()
            if d is not None:                             # the database accepted the proof but the in-process side refused it
                self._idp._end(d["token"])
            raise
        d = self._idp._take()
        if d is None:                                     # unreachable: issue() succeeded only through PgIdentity.authenticate
            self._sessions.end(s)
            raise AuthenticationFailed("no database session")
        try:
            if (s.tenant, s.principal_id, s.channel) != (d["tenant"], d["principal"], d["channel"]):
                raise AuthenticationFailed("database and in-process sessions name different identities")
            self._sessions._bind(s, PG_TOKEN, d["token"])
        except BaseException:
            self._idp._end(d["token"])
            self._sessions.end(s)
            raise
        return PgSession(s, d["token"])

    def end(self, session) -> None:
        """Logout: the database token is refused from now on, and the in-process Session is no longer live."""
        if not isinstance(session, PgSession):
            raise TypeError("PgSession")
        self._idp._end(session.db_token)
        self._sessions.end(session.session)


class _SnapEngine:
    """One requirement's raw state as dc.explain returned it, in the shape DecisionService.explain reads."""

    def __init__(self, raw):
        self._raw = raw

    def _revoked(self):
        return []

    def _participant(self, pid, req):
        return True                                   # the database already checked participation for this session

    def _head(self, req):
        return self._raw["head"]

    def _open_items(self, req):
        return self._raw["open"]


class _SnapVault:
    def __init__(self, raw, tenant):
        self._vals, self._tenant = raw["values"], tenant

    def _get(self, tenant, ref):
        v = self._vals.get(ref) if isinstance(ref, str) and tenant == self._tenant else None
        return None if v is None else {"tenant": tenant, "text": v["text"], "label": Label.from_dict(v["label"])}


class _Refused(Exception):
    """The in-process judgment after a database call refused (final review P1): rolls that call's transaction back."""


class PgDecisionService:
    """DecisionService over the database API, as the request handler (dc_app) uses it.

    Every entry point (submit, explain, table, erase_subject) first verifies the PgSession and gets its token from the SessionAuthority,
    BEFORE any database call: the in-process Session must be live (verified by identity) and the PgSession's token must equal the token
    attached to that Session when the bridge issued it (review-3 N1). The database then derives tenant, principal and channel from that
    token's session row, so the in-process check and the database act as the same principal, channel and tenant. Final review P1: the
    database call can wait, and the in-process authority (the in-process session, the directory, the policy document's revocations) is
    invisible to the database's own final judgment, so it is judged again AFTER the call: a writing call (dc.submit, dc.erase_values)
    runs in its own transaction and is rolled back when that judgment refuses (`_judged`); a read is rendered by the in-process
    DecisionService, which judges after its reads.

    submit asks THE revocation source (pol.enforce.Revocations) exactly where the memory service does (review-3 N3): a principal revoked
    by the policy document (skeleton over-match included) or by a core revoke event is refused principal_revoked. LIMIT: the policy
    document lives in process; a caller that bypasses this service with raw SQL as dc_app and holds a still-valid token of a principal
    revoked ONLY by the policy document is stopped by the database only after a core `revoke` event, a dc.end_session of that token,
    or at the token's expiry: the lifetime dc_idp chose at dc.open_session (1 to 86400 s; PgIdentity's default is 3600 s), judged on
    the wall clock and refused inside REPEATABLE READ or SERIALIZABLE transactions (delta review M1). The deployment must end a
    principal's sessions when it revokes that principal in the policy document, or accept that window."""

    def __init__(self, enforcer, app_conn):
        from .enforce import Enforcer
        if not isinstance(enforcer, Enforcer) or app_conn is None:
            raise TypeError("PgDecisionService(enforcer, app_conn)")
        self._enf, self._c = enforcer, app_conn

    def _verified(self, session):
        """(principal, database token) for a PgSession whose two parts belong together, else None. No database call."""
        from .enforce import PolicyDenied
        if not isinstance(session, PgSession) or not isinstance(session.db_token, str):
            return None
        try:
            p = self._enf.principal_of(session.session)
        except PolicyDenied:
            return None
        bound = self._enf._sessions._bound(session.session, PG_TOKEN)
        if not isinstance(bound, str) or not hmac.compare_digest(bound.encode(), session.db_token.encode()):
            return None
        return p, bound

    def _judged(self, session, pid, sql, args, also=None):
        """One writing API call in its own transaction, judged in process again after it and before COMMIT (final review P1). Returns
        (result, None), or (None, refusal code) with the call rolled back. Nothing after this judgment waits (COMMIT takes no lock).
        `also`, when given, is a further in-process judgment (no database call) run after the session and revocation checks; it returns
        a refusal code or None (erase_subject: each value's erase decision)."""
        try:
            with self._c.transaction():
                out = self._c.execute(sql, args).fetchone()[0]
                v = self._verified(session)
                if v is None or v[0].id != pid:
                    raise _Refused("unauthenticated:invalid_session")
                if self._enf._revocations._is_revoked(pid):
                    raise _Refused("principal_revoked")
                why = also() if also is not None else None
                if why is not None:
                    raise _Refused(why)
                return out, None
        except _Refused as e:
            return None, e.args[0]

    def _erase_decision(self, session, label):
        """None when the in-process policy point allows erasing a value with this label now, else the counter it goes to."""
        from .enforce import PolicyDenied
        try:
            self._enf._check(session.session, "erase", [label], {})
            return None
        except PolicyDenied as e:
            return "queued" if e.decision.reason == "erasure_queued" else "denied"

    def _changed(self, session, labels, refs, changed):
        """After dc.erase_values, before COMMIT: decide each value again; records in `changed` every ref the policy point no longer
        allows (a legal hold recorded, the erase grant withdrawn while the call waited)."""
        for ref in refs:
            why = self._erase_decision(session, labels[ref])
            if why is not None:
                changed[ref] = why
        return "erase_decision_changed" if changed else None

    def _refuse(self, pid, code, body):
        t = body.get("type") if isinstance(body, dict) else None
        self._enf._audit.add({"action": "decision", "principal": pid, "allow": False, "reason": code, "type": t,
                              "req": body.get("req") if isinstance(body, dict) else None, "policy_version": self._enf._ver()})
        return {"code": code, "original": None, "id": None, "value_ref": None}

    def submit(self, session, body) -> dict:
        v = self._verified(session)
        if v is None:
            return {"code": "unauthenticated:invalid_session", "original": None, "id": None, "value_ref": None}
        p, tok = v
        if not isinstance(body, dict):
            return self._refuse(p.id, "bad_event", {})
        if self._enf._revocations._is_revoked(p.id):                  # as DecisionService.submit (R3-10, review-3 N3)
            return self._refuse(p.id, "principal_revoked", body)
        try:
            out, why = self._judged(session, p.id, "select dc.submit(%s, %s)", (tok, Jsonb(body)))
        except (TypeError, ValueError):
            out, why = {"code": "bad_event", "original": None, "id": None, "value_ref": None}, None
        if why is not None:
            return self._refuse(p.id, why, body)
        if out.get("id") and not out["id"].startswith(p.id + "/"):
            raise RuntimeError("database session and in-process session name different principals")      # unreachable after _verified
        if out["code"] == "revoked":
            self._enf._revocations._add(body.get("target"))
        self._enf._audit.add({"action": "decision", "principal": p.id, "allow": out["code"] in _core.APPLIED, "reason": out["code"],
                              "type": body.get("type"), "policy_version": self._enf._ver()})
        return out

    def _render(self, session, raw):
        from .decisions import DecisionService, Vault
        if raw is None or raw.get("principal") != session.session.principal_id:
            return None
        svc = DecisionService(self._enf, {raw["tenant"]: _SnapEngine(raw)}, Vault())
        svc._vault = _SnapVault(raw, raw["tenant"])
        return svc.explain(session.session, raw["req"])

    def explain(self, session, req):
        v = self._verified(session)
        if v is None or not isinstance(req, str):
            return None
        raw = self._c.execute("select dc.explain(%s, %s)", (v[1], req)).fetchone()[0]
        return self._render(session, raw)

    def table(self, session):
        v = self._verified(session)
        if v is None:
            return []
        raws = self._c.execute("select dc.read_table(%s)", (v[1],)).fetchone()[0]
        return [x for x in (self._render(session, r) for r in raws) if x is not None]

    def erase_subject(self, session, subject) -> dict:
        """As DecisionService.erase_subject. The database lists the values about the canonical subject (admin session only); the in-process
        policy point decides each (a legal hold queues it) for the SAME verified session; the database deletes the allowed ones and, when
        none about the subject remains, the token mapping, under the tenant lock held exclusively (no concurrent submit can mint or use
        the token meanwhile). Final review P1: the in-process decisions are made again after dc.erase_values (which can wait) and
        before COMMIT; when a value's decision changed during the wait the call is rolled back, that value is counted as queued or denied
        (as the memory engine judges each value at its deletion), and the remaining values are erased in a fresh call, judged again.
        Each such round removes at least one value, so the loop ends. A value denied before the call and allowed after it stays (the
        result is "incomplete"; a later erasure takes it)."""
        v = self._verified(session)
        none = {"erased": 0, "queued": 0, "denied": 0, "refused": "unauthorized", "result": None}
        if v is None:
            return none
        p, tok = v
        try:
            subj = canon_subject(subject)
        except LabelError:
            return dict(none, refused="bad_subject")
        cand = self._c.execute("select dc.erase_candidates(%s, %s)", (tok, subj)).fetchone()[0]
        if cand["refused"]:
            return dict(none, refused=cand["refused"])
        out = {"erased": 0, "queued": 0, "denied": 0, "refused": None, "result": None}
        labels = {x["ref"]: Label.from_dict(x["label"]) for x in cand["values"]}
        allowed = []
        for ref, lab in labels.items():
            why = self._erase_decision(session, lab)
            if why is None:
                allowed.append(ref)
            else:
                out[why] += 1
        while True:
            changed = {}
            res, why = self._judged(session, p.id, "select dc.erase_values(%s, %s, %s)", (tok, subj, allowed),
                                    also=lambda: self._changed(session, labels, allowed, changed))
            if why != "erase_decision_changed":
                break
            for ref, counter in changed.items():
                out[counter] += 1
            allowed = [r for r in allowed if r not in changed]
        if why is not None:
            return dict(none, refused="unauthorized")
        if res.get("refused"):
            return dict(none, refused=res["refused"])
        out["erased"] = res["erased"]
        if not cand["values"]:
            out["result"] = "no_values_found"
        else:
            out["result"] = "erased" if out["queued"] == out["denied"] == 0 and res["remaining"] == 0 else "incomplete"
        self._enf._audit.add({"action": "erase_subject", "principal": p.id, "allow": True, "reason": out["result"], "erased": out["erased"],
                              "queued": out["queued"], "denied": out["denied"], "policy_version": self._enf._ver()})
        return out


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
    """ok only when the chain verifies inside the database, at least ONE anchor of this tenant exists, and every anchored (n, hash) is still
    the event at chain_n = n. Zero anchors is not ok (nothing outside the database was ever compared: review-3, R3-8).

    What it protects: an owner who rewrote history and recomputed the hashes is detected for any event up to the LAST export
    (`anchored_through`), because the recomputed hash of an anchored event differs as soon as any earlier byte changed.
    What it does not: events after the last export (`unanchored_suffix` of them) are protected only by the in-database chain, which the
    owner can recompute; and the anchor is only as strong as its storage. FileAnchor is an ordinary append-only file: whoever can write
    it (the same host's owner) can rewrite it too. Point it at storage the database owner cannot write, or implement Anchor over a WORM
    store, and export often."""
    recs = [x for x in anchor.records() if x.get("tenant") == tenant]
    n_ok, chain_ok = conn.execute("select n, ok from dc.verify_chain(%s)", (tenant,)).fetchone()
    bad = []
    for x in recs:
        row = conn.execute("select hash from dc.events where tenant = %s and chain_n = %s", (tenant, x["n"])).fetchone()
        if row is None:
            bad.append({"n": x["n"], "problem": "missing", "anchored": x["hash"]})
        elif row[0] != x["hash"]:
            bad.append({"n": x["n"], "problem": "hash_differs", "anchored": x["hash"], "found": row[0]})
    through = max((int(x["n"]) for x in recs), default=0)
    return {"ok": bool(chain_ok) and bool(recs) and not bad, "chain_ok": bool(chain_ok), "events": int(n_ok), "anchored": len(recs),
            "anchored_through": through, "unanchored_suffix": max(int(n_ok) - through, 0), "mismatches": bad}
