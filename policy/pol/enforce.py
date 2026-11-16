"""Enforcement at the choke points. Nothing here takes a principal, a tenant, a clock, a label of stored data, or a recipient object from a
caller:

- Enforcer(policy_source, directory, sessions, audit): every argument is required. The clock is the runtime's (time.time); there is no
  parameter for it. Each check verifies the Session by identity with the SessionAuthority, reads the principal from the Directory by
  the session's principal id (a missing entry denies `unknown_principal`; a directory that raises denies `directory_error`), reads the
  policy from policy_source at that moment, and logs the decision with its policy version to the audit log.
- Store holds labelled records. Ids are assigned by the store. A label is supplied only for NEW content (append) and must be at least the
  join of its lineage; derived content (append_derived, model answers) gets the join of its inputs, never a caller label. A read of a
  missing record and a denied read return the same refusal to the caller (the audit log keeps the real reason).
- Guarded ports (model, embedder, export) and the Outbox take record ids and read text and CURRENT labels from the store. The Outbox
  queues references, re-reads labels and existence at delivery (mandatory: there is no option to skip it), resolves the recipient from
  the directory ONCE per delivery and hands that same object to the decision and to the sink. Erasing a record purges queued items.
PolicyDenied is not a port error, so no failover may retry it on another endpoint."""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ._pdp import Decision as _Decision, evaluate as _evaluate
from .directory import Directory as _Directory, SessionAuthority as _SessionAuthority
from .ids import canon, skeleton
from .labels import Label, join, leq
from .policy import Policy


class PolicyDenied(Exception):
    retryable = False

    def __init__(self, decision: _Decision):
        super().__init__(decision.reason)
        self.decision = decision


class AuditLog:
    """Append-only, thread-safe log of every decision (allow and deny) with its policy version. In memory here; growth is one row per
    checked request (bounded by request admission, which is outside this module)."""

    def __init__(self):
        self._rows: List[dict] = []
        self._lock = threading.Lock()

    def add(self, row: dict):
        with self._lock:
            self._rows.append(dict(row, seq=len(self._rows)))

    def _snapshot(self) -> List[dict]:
        """Lab and test access only. Not a public read path: a deployment exports audit rows through its admin channel."""
        with self._lock:
            return [dict(r) for r in self._rows]


class Revocations:
    """THE revocation source (R3-10): a principal is revoked if the current policy document lists it (skeleton match, as the PDP does) or
    an applied core `revoke` event named it. Every enforcer decision and every decision-service submission asks this one object, so a
    revocation from either side reaches stores, ports, the outbox and the decision core alike. The core's own revoked set is a replica
    derived from its event log (it must rebuild deterministically from the log)."""

    def __init__(self, policy_source):
        self._policy_source = policy_source
        self._live = set()
        self._lock = threading.Lock()

    def _add(self, pid):
        with self._lock:
            self._live.add(canon(pid))

    def _is_revoked(self, pid) -> bool:
        try:
            k = canon(pid)
        except Exception:       # noqa: BLE001  a malformed id is not a principal: treat as revoked (deny)
            return True
        with self._lock:
            if k in self._live:
                return True
        try:
            revoked = self._policy_source().revoked
        except Exception:       # noqa: BLE001  no readable policy: the PDP denies anyway
            return False
        sk = skeleton(k)
        return any(skeleton(r) == sk for r in revoked)


class Enforcer:
    def __init__(self, policy_source: Callable[[], Policy], directory: _Directory, sessions: _SessionAuthority, audit: AuditLog):
        if not callable(policy_source) or not isinstance(directory, _Directory) or not isinstance(sessions, _SessionAuthority) or not isinstance(audit, AuditLog):
            raise TypeError("Enforcer needs a policy source, a Directory, a SessionAuthority and an AuditLog")
        self._policy_source, self._dir, self._sessions, self._audit = policy_source, directory, sessions, audit
        self._clock = time.time
        self._revocations = Revocations(policy_source)
        self._tickets, self._tlock = {}, threading.Lock()
        self._disabled = set()          # destinations disabled by a pin-drift incident (R3-6); re-enabling is an operator action

    def policy(self) -> Policy:
        return self._policy_source()

    def _deny(self, reason, action, pid, ver, extra=None):
        d = _Decision(False, reason, ver, action if isinstance(action, str) else "")
        self._audit.add(dict(extra or {}, action=d.action, principal=pid, allow=False, reason=reason, policy_version=ver))
        raise PolicyDenied(d)

    def _ver(self):
        try:
            return getattr(self._policy_source(), "version", None)
        except Exception:       # noqa: BLE001
            return None

    def _lookup(self, pid):
        try:
            return self._dir.lookup(pid), None
        except Exception as e:      # noqa: BLE001  a failing directory denies
            return None, "directory_error:" + type(e).__name__

    def principal_of(self, session):
        """The CURRENT directory entry behind a valid session, or PolicyDenied."""
        ver = self._ver()
        if not self._sessions.verify(session):
            self._deny("invalid_session", "session", None, ver)
        p, err = self._lookup(session.principal_id)
        if err:
            self._deny(err, "session", session.principal_id, ver)
        if p is None:
            self._deny("unknown_principal", "session", session.principal_id, ver)
        if p.tenant != session.tenant:
            self._deny("session_tenant_mismatch", "session", session.principal_id, ver)
        return p

    def _check(self, session, action, labels, request, recipient_id=None):
        """Decision for the principal behind a VERIFIED session. Returns (decision, principal, recipient snapshot)."""
        p = self.principal_of(session)
        dest = request.get("destination") if isinstance(request, dict) else None
        return _decide_verified(self, p, action, labels, request, recipient_id, dest)

    def _ticket(self, session, action, labels, request, recipient_id=None):
        """Check now (verified session) and mint a single-use ticket for a later re-check at delivery. A ticket can only be minted here,
        is recognised by identity, and is consumed by `_redeem`; no decision path takes a principal id (R3-1)."""
        d, p, rcp = self._check(session, action, labels, request, recipient_id)
        t = _Ticket(p.id, p.tenant, action, dict(request) if isinstance(request, dict) else {}, recipient_id,
                    request.get("destination") if isinstance(request, dict) else None, d.policy_version)
        with self._tlock:
            self._tickets[id(t)] = t
        return d, p, rcp, t

    def _discard(self, ticket):
        with self._tlock:
            if self._tickets.get(id(ticket)) is ticket:
                del self._tickets[id(ticket)]

    def _redeem(self, ticket, labels):
        """Re-decide a queued item at delivery: the ticket must be one this Enforcer minted and not yet used; the sender is read from
        the directory again (removed or moved principals deny)."""
        ver = self._ver()
        with self._tlock:
            ok = self._tickets.get(id(ticket)) is ticket
            if ok:
                del self._tickets[id(ticket)]
        if not ok:
            self._deny("invalid_ticket", "ticket", None, ver)
        p, err = self._lookup(ticket.sender)
        if err:
            self._deny(err, ticket.action, ticket.sender, ver)
        if p is None:
            self._deny("unknown_principal", ticket.action, ticket.sender, ver)
        if p.tenant != ticket.tenant:
            self._deny("session_tenant_mismatch", ticket.action, ticket.sender, ver)
        return _decide_verified(self, p, ticket.action, labels, ticket.request, ticket.recipient_id, ticket.dest)


class _Ticket:
    __slots__ = ("sender", "tenant", "action", "request", "recipient_id", "dest", "enqueue_version")

    def __init__(self, sender, tenant, action, request, recipient_id, dest, enqueue_version):
        self.sender, self.tenant, self.action, self.request = sender, tenant, action, request
        self.recipient_id, self.dest, self.enqueue_version = recipient_id, dest, enqueue_version


def _decide_verified(enf, p, action, labels, request, recipient_id=None, dest=None):
    """The decision itself, for a principal `p` that Enforcer._check or Enforcer._redeem has just read from the directory for a verified
    session or ticket. Module-private: it is reached only through those two methods (Python cannot enforce this; tests/test_surface.py
    proves by call that no exported callable decides without a verified session)."""
    try:
        pol = enf._policy_source()
    except Exception:       # noqa: BLE001
        pol = None
    ver = getattr(pol, "version", None)
    extra = {"dest": dest}
    try:
        extra["label"] = join(labels).digest()
    except Exception:       # noqa: BLE001
        extra["label"] = None
    rcp = None
    if dest is not None and dest in enf._disabled:
        enf._deny("destination_disabled", action, p.id, ver, extra)
    if enf._revocations._is_revoked(p.id):
        enf._deny("principal_revoked", action, p.id, ver, extra)
    if action == "send_inbox":
        if not isinstance(recipient_id, str):
            enf._deny("recipient_required", action, p.id, ver, extra)
        rcp, err = enf._lookup(recipient_id)
        if err:
            enf._deny(err, action, p.id, ver, extra)
        if rcp is None:
            enf._deny("recipient_unknown", action, p.id, ver, extra)
        if enf._revocations._is_revoked(rcp.id):
            enf._deny("recipient_revoked", action, p.id, ver, extra)
    d = _evaluate(p, action, labels, request, pol, float(enf._clock()), rcp)
    enf._audit.add(dict(extra, action=d.action, principal=p.id, allow=d.allow, reason=d.reason, policy_version=d.policy_version,
                        recipient=None if rcp is None else rcp.id))
    if not d.allow:
        raise PolicyDenied(d)
    return d, p, rcp


@dataclass(frozen=True)
class Record:
    id: str
    tenant: str
    text: str
    label: Label
    lineage: Tuple[str, ...]


def _req(request):
    if request is None:
        return {}
    if not isinstance(request, dict):
        return {"__not_a_dict__": True}
    return dict(request)


class Store:
    def __init__(self, enforcer: Enforcer):
        if not isinstance(enforcer, Enforcer):
            raise TypeError("Store needs an Enforcer")
        self._enf = enforcer
        self._recs: Dict[str, Record] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str], None]] = []
        self.erasure_queue: List[Tuple[str, str]] = []           # (principal id, record id): erasures held by a recorded legal hold

    def _get(self, rid) -> Optional[Record]:
        with self._lock:
            return self._recs.get(rid) if isinstance(rid, str) else None

    def _put(self, tenant, text, label, lineage) -> str:
        with self._lock:
            rid = "r%d" % next(self._ids)
            self._recs[rid] = Record(rid, tenant, text, label, tuple(lineage))
        return rid

    def _pid(self, session):
        return getattr(session, "principal_id", None)

    def _sources(self, session, refs, action):
        if isinstance(refs, (str, bytes)):
            self._enf._deny("refs_type", action, self._pid(session), self._enf._ver())
        recs = []
        for rid in refs:
            r = self._get(rid)
            if r is None:
                self._enf._deny("not_found_or_denied", action, self._pid(session), self._enf._ver())
            recs.append(r)
        return recs

    def append(self, session, text: str, label: Label, lineage: Sequence[str] = ()) -> str:
        if not isinstance(text, str) or not isinstance(label, Label):
            self._enf._deny("bad_record", "append", self._pid(session), self._enf._ver())
        src = self._sources(session, lineage, "append")
        if src and not leq(join(r.label for r in src), label):
            self._enf._deny("label_below_lineage", "append", self._pid(session), self._enf._ver())
        _d, p, _r = self._enf._check(session, "append", [label] + [r.label for r in src], {})
        return self._put(p.tenant, text, label, [r.id for r in src])

    def append_derived(self, session, text: str, lineage: Sequence[str]) -> str:
        src = self._sources(session, lineage, "append")
        if not src or not isinstance(text, str):
            self._enf._deny("derived_needs_lineage", "append", self._pid(session), self._enf._ver())
        label = join(r.label for r in src)
        _d, p, _r = self._enf._check(session, "append", [label], {})
        return self._put(p.tenant, text, label, [r.id for r in src])

    def read(self, session, rid: str) -> Record:
        r = self._get(rid)
        try:
            if r is None:
                self._enf._deny("not_found", "read", self._pid(session), self._enf._ver())
            self._enf._check(session, "read", [r.label], {})
        except PolicyDenied as e:
            raise PolicyDenied(_Decision(False, "not_found_or_denied", e.decision.policy_version, "read"))
        return r

    def context(self, session, refs: Sequence[str], destination: str) -> Tuple[List[str], int]:
        """Context assembly for a prompt to `destination`: the refs the principal may inject; the rest are withheld (count only)."""
        ok, withheld = [], 0
        for rid in refs:
            r = self._get(rid)
            try:
                if r is None:
                    raise PolicyDenied(_Decision(False, "not_found", None, "inject"))
                self._enf._check(session, "inject", [r.label], {"destination": destination})
                ok.append(rid)
            except PolicyDenied:
                withheld += 1
        return ok, withheld

    def erase(self, session, rid: str) -> str:
        r = self._get(rid)
        try:
            if r is None:
                self._enf._deny("not_found", "erase", self._pid(session), self._enf._ver())
            self._enf._check(session, "erase", [r.label], {})
        except PolicyDenied as e:
            if e.decision.reason == "erasure_queued":
                with self._lock:
                    self.erasure_queue.append((session.principal_id, rid))
                return "queued"
            raise
        with self._lock:
            self._recs.pop(rid, None)
            listeners = list(self._listeners)
        for fn in listeners:
            fn(rid)
        return "erased"

    def _subscribe(self, fn):
        with self._lock:
            self._listeners.append(fn)


class GuardedChat:
    """Front of a ChatModel (duck-typed memcore.ports.ChatModel). The answer is stored as a derived record (join of the inputs' labels).
    Every policy check (declared model, bans, allow-list, region, sensitivity, local-only, restricted) runs BEFORE the call, against the
    declared id, and policy parsing guarantees that id is an immutable pinned snapshot (R3-6). The served-model comparison after the call
    is a DETECTOR only: the request has already reached the provider by then. A mismatch is an incident: the answer is dropped
    (`pin_drift`) and the destination is disabled for every later request (`destination_disabled`) until an operator re-enables it."""

    def __init__(self, inner, enforcer: Enforcer, store: Store, dest_id: str):
        self._inner, self._enf, self._store, self._dest = inner, enforcer, store, dest_id

    def complete(self, session, refs: Sequence[str], system_refs: Sequence[str] = ()) -> Record:
        src = self._store._sources(session, list(system_refs) + list(refs), "send_model")
        sysn = len(system_refs)
        _d, p, _r = self._enf._check(session, "send_model", [r.label for r in src], {"destination": self._dest})
        msgs = [{"role": "user", "content": "\n".join(r.text for r in src[sysn:])}]
        res = self._inner.complete(msgs, system="\n".join(r.text for r in src[:sysn]) or None)
        pol = self._enf.policy()
        want = pol.resolve_model(pol.destinations[self._dest].model)
        if want is None or pol.resolve_model(getattr(res, "model", None) or "") != want:
            self._enf._disabled.add(self._dest)
            self._enf._deny("pin_drift", "send_model", p.id, pol.version, {"dest": self._dest})
        rid = self._store.append_derived(session, res.text, [r.id for r in src])
        return self._store._get(rid)


class GuardedEmbedder:
    def __init__(self, inner, enforcer: Enforcer, store: Store, dest_id: str):
        self._inner, self._enf, self._store, self._dest = inner, enforcer, store, dest_id

    def embed(self, session, refs: Sequence[str], kind: str = "document"):
        """(vectors, label): vectors carry the join of their inputs' labels (a vector is derived data)."""
        src = self._store._sources(session, refs, "embed")
        self._enf._check(session, "embed", [r.label for r in src], {"destination": self._dest})
        return self._inner.embed([r.text for r in src], kind), join(r.label for r in src)


class GuardedExport:
    def __init__(self, export_fn: Callable[[str, str, str], None], enforcer: Enforcer, store: Store, dest_id: str):
        self._fn, self._enf, self._store, self._dest = export_fn, enforcer, store, dest_id

    def export(self, session, refs: Sequence[str]):
        src = self._store._sources(session, refs, "export")
        _d, p, _r = self._enf._check(session, "export", [r.label for r in src], {"destination": self._dest})
        for r in src:
            self._fn(self._dest, p.tenant, r.text)


class Outbox:
    """Queued delivery for send_inbox, send_model and export. sink(sender: Principal, recipient: Principal or None, texts, request)."""

    def __init__(self, enforcer: Enforcer, store: Store, action: str, sink: Callable):
        if action not in ("send_inbox", "send_model", "export") or not isinstance(enforcer, Enforcer) or not isinstance(store, Store) or not callable(sink):
            raise TypeError("Outbox(enforcer, store, action, sink)")
        self._enf, self._store, self._action, self._sink = enforcer, store, action, sink
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self.delivered: List[dict] = []
        self.refused: List[dict] = []
        store._subscribe(self._purge)

    def _purge(self, rid):
        with self._lock:
            keep = []
            for it in self._queue:
                if rid in it["refs"]:
                    it["refusal"] = "source_erased"
                    self._enf._discard(it.pop("ticket"))
                    self.refused.append(it)
                else:
                    keep.append(it)
            self._queue = keep

    def enqueue(self, session, refs: Sequence[str], request: Optional[dict] = None):
        rq = _req(request)
        rid = rq.pop("recipient_id", None)
        src = self._store._sources(session, refs, self._action)
        d, p, _r, ticket = self._enf._ticket(session, self._action, [r.label for r in src], rq, rid)
        with self._lock:
            self._queue.append({"sender": p.id, "tenant": p.tenant, "refs": [r.id for r in src], "request": rq, "recipient_id": rid,
                                "enqueue_version": d.policy_version, "ticket": ticket})

    def deliver_all(self):
        with self._lock:
            q, self._queue = self._queue, []
        for it in q:
            ticket = it.pop("ticket")
            try:
                recs = []
                for rid in it["refs"]:                       # labels and existence as they are NOW
                    r = self._store._get(rid)
                    if r is None:
                        self._enf._discard(ticket)
                        self._enf._deny("source_erased_or_missing", self._action, it["sender"], self._enf._ver())
                    recs.append(r)
                d, sender, rcp = self._enf._redeem(ticket, [r.label for r in recs])
                self._sink(sender, rcp, [r.text for r in recs], dict(it["request"]))          # the very objects the decision used
                it["delivery_version"] = d.policy_version
                self.delivered.append(it)
            except PolicyDenied as e:
                it["refusal"] = e.decision.reason
                self.refused.append(it)
        return self.delivered, self.refused
