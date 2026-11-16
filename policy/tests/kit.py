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

    def __init__(self, principals=None, policy=None, core_config=None):
        self.dir = Directory(principals or [P("alice"), P("bob"), P("carol", clearance=0, groups=("other",)),
                                            P("bot", kind="agent", model="local"), P("ann", kind="analyst"), P("admin")])
        self.pol = policy or parse_policy(policy_doc())
        self.idp = LabIdP()
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
