"""Tests-only helpers. Production code may not import this module (tests/surface.py rule S5 and tests/test_surface.py enforce it).

The only way a test changes the clock is FixedClock, which replaces the Enforcer's private runtime clock for the duration of a
`with` block; there is no constructor or call parameter for it."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field

from pol import _core
from pol.decisions import DecisionService, MemoryEngine, Vault
from pol.directory import Directory, Principal, SessionAuthority
from pol.identity import Assertion
from pol.enforce import AuditLog, Enforcer, Store
from pol.labels import Label
from pol.policy import parse_policy

T = "tenant-a"


class LabIdP:
    """TEST DOUBLE of pol.identity.IdentityAdapter (tests and experiments only). A credential is an HMAC-signed JSON of (principal,
    channel, factor, nonce, expiry) under a key generated per instance; it is single use and expires. The channel is fixed when the
    credential is minted (enrolment), so a session's channel is never a caller's choice."""

    def __init__(self, ttl: float = 60.0):
        self._key = secrets.token_bytes(32)
        self._used = set()
        self._lock = threading.Lock()
        self._ttl = ttl

    def credential(self, pid, channel, factor=None, expires_at=None) -> str:
        body = json.dumps({"p": pid, "c": channel, "f": factor or "%s@%s" % (pid, channel), "n": secrets.token_hex(8),
                           "x": time.time() + self._ttl if expires_at is None else expires_at}, sort_keys=True)
        return body + "." + hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def authenticate(self, credential) -> Assertion:
        body, _, mac = str(credential).rpartition(".")
        if not hmac.compare_digest(mac, hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()):
            raise ValueError("bad signature")
        d = json.loads(body)
        if d["x"] < time.time():
            raise ValueError("expired")
        with self._lock:
            if d["n"] in self._used:
                raise ValueError("replayed")
            self._used.add(d["n"])
        return Assertion(d["p"], d["c"], d["f"])


@dataclass(frozen=True)
class LabFactor:
    """TEST DOUBLE of an enrolled device or key (step 2): the factor id the database stores and the HMAC key the device holds. The key
    is returned once by dc.enroll_factor (owner only) and never printed: repr hides it, and results record the factor id only."""
    factor_id: str
    tenant: str
    principal: str
    channel: str
    key: bytes = field(repr=False)


def enroll(owner_conn, tenant, principal, channel) -> LabFactor:
    """Admin path (database owner or superuser): enrol a factor for (tenant, principal, channel) through dc.enroll_factor."""
    d = owner_conn.execute("select dc.enroll_factor(%s, %s, %s)", (tenant, principal, channel)).fetchone()[0]
    return LabFactor(d["factor"], tenant, principal, channel, bytes.fromhex(d["key"]))


def proof(f: LabFactor, expires_in: float = 60.0, nonce=None, key=None, **override) -> str:
    """An authentication proof as the device makes it: canonical JSON of (tenant, principal, channel, factor, nonce, expires) and
    HMAC-SHA256 under the factor's key. `override` replaces body fields (forgery tests); `key` signs with another key."""
    body = {"tenant": f.tenant, "principal": f.principal, "channel": f.channel, "factor": f.factor_id,
            "nonce": nonce or secrets.token_hex(16), "expires": int(time.time() + expires_in)}
    body.update(override)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return text + "." + hmac.new(f.key if key is None else key, text.encode(), hashlib.sha256).hexdigest()


def pg_token(owner_conn, idp_conn, tenant, principal, channel, ttl_s: int = 60) -> str:
    """A database session token the deployment way: enrol a factor (owner), make its proof (device), open the session (dc_idp)."""
    return idp_conn.execute("select dc.open_session(%s, %s)", (proof(enroll(owner_conn, tenant, principal, channel)), ttl_s)).fetchone()[0]["token"]


class PgLab:
    """A World whose SessionAuthority verifies proofs through the database (pol.pgcore.PgIdentity), with its PgSessionBridge. s(pid)
    issues a PgSession from a fresh proof of a factor enrolled on first use for (principal, channel)."""

    def __init__(self, owner_conn, idp_conn, ttl_s: int = 60, **world_kw):
        from pol import pgcore
        self._o, self._f = owner_conn, {}
        self.w = World(identity=pgcore.PgIdentity(idp_conn, ttl_s), **world_kw)
        self.bridge = pgcore.PgSessionBridge(self.w.sessions)

    def factor(self, pid, channel=None) -> LabFactor:
        p = self.w.dir.lookup(pid)
        tenant = p.tenant if p is not None else T
        ch = channel or ("owner_ui" if p is None or p.kind == "human" else "agent_api")
        if (pid, ch) not in self._f:
            self._f[(pid, ch)] = enroll(self._o, tenant, pid, ch)
        return self._f[(pid, ch)]

    def s(self, pid, channel=None):
        return self.bridge.issue(proof(self.factor(pid, channel)))


class FixedClock:
    def __init__(self, enforcer, t: float):
        self.enf, self.t = enforcer, float(t)

    def __enter__(self):
        self.old = self.enf._clock
        self.enf._clock = lambda: self.t
        return self

    def __exit__(self, *a):
        self.enf._clock = self.old


def P(pid, kind="human", tenant=T, groups=("staff",), clearance=3, restricted_ok=False, region="eu", model=None):
    return Principal(pid, tenant, frozenset(groups), clearance, restricted_ok, kind, region, model)


def L(**kw):
    kw.setdefault("tenants", [T])
    return Label(**kw)


def policy_doc(**over):
    doc = {"version": 1,
           "grants": {T: ["append", "read", "inject", "send_model", "embed", "send_inbox", "export", "erase"],
                      "tenant-b": ["append", "read"]},
           "models": {"m-local@1": {"family": "m-local", "version": "1"}, "gpt-x@1": {"family": "gpt-x", "version": "1"},
                      "gpt-x_2025": {"family": "gpt-x", "version": "2025"}, "gpt-x@2025-01": {"family": "gpt-x", "version": "2025-01"},
                      "gpt-x@2025-01:eu": {"family": "gpt-x", "version": "2025-01"}, "gpt-xl@1": {"family": "gpt-xl", "version": "1"}},
           "destinations": {"local": {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-local@1", "tenants": [T]},
                            "cloud": {"kind": "model", "region": "eu", "local": False, "max_sensitivity": 2, "model": "gpt-x@1", "tenants": [T]},
                            "emb": {"kind": "embedder", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-local@1", "tenants": [T]},
                            "exp": {"kind": "export", "region": "eu", "local": False, "max_sensitivity": 1, "model": None, "tenants": [T]}}}
    doc.update(over)
    return doc


class World:
    """A wired deployment: directory, sessions, audit, enforcer, store, decision service (memory engine)."""

    def __init__(self, principals=None, policy=None, core_config=None, identity=None):
        """identity: the identity adapter the SessionAuthority verifies credentials with (default: a fresh LabIdP; the Postgres tests
        pass pol.pgcore.PgIdentity, whose credentials are database-verified proofs)."""
        self.dir = Directory(principals or [P("alice"), P("bob"), P("carol", clearance=0, groups=("other",)),
                                            P("bot", kind="agent", model="local"), P("ann", kind="analyst"), P("admin")])
        self.pol = policy or parse_policy(policy_doc())
        self.idp = identity if identity is not None else LabIdP()
        self.sessions = SessionAuthority(self.dir, self.idp)
        self.audit = AuditLog()
        self.enf = Enforcer(lambda: self.pol, self.dir, self.sessions, self.audit)
        self.store = Store(self.enf)
        self.vault = Vault()
        cfg = core_config or {"tenant": T, "principals": {"alice": "human", "bob": "human", "carol": "human", "bot": "agent", "ann": "analyst",
                                                          "admin": "human"},
                              "authority": {"r1": ["alice", "bob", "bot"], "r2": ["alice", "carol"]}, "proposers": {"r1": ["ann"]},
                              "admins": ["admin"]}
        self.engine = MemoryEngine(cfg)
        self.svc = DecisionService(self.enf, {T: self.engine}, self.vault)

    def s(self, pid, channel=None):
        p = self.dir.lookup(pid)
        return self.sessions.issue(self.idp.credential(pid, channel or ("owner_ui" if p.kind == "human" else "agent_api")))

    def lab(self, **kw):
        return L(**kw).to_dict()

    def propose(self, sess, cid, text, based_on=0, req="r1", **lab):
        return self.svc.submit(sess, {"type": "propose", "client_id": cid, "idem": "k-" + cid, "req": req, "value": text, "label": self.lab(**lab),
                                      "based_on": based_on})

    def decide(self, sess, cid, verb, prop_id, based_on, req="r1"):
        it = next(x for x in self.svc.explain(sess, req)["open"] if x["id"] == prop_id)
        return self.svc.submit(sess, {"type": "decide", "client_id": cid, "idem": "k-" + cid, "req": req, "verb": verb, "proposal": prop_id,
                                      "proposal_hash": it["proposal_hash"], "based_on": based_on})


def phash(pid, ref, author):
    return _core.proposal_hash(pid, ref, author)
