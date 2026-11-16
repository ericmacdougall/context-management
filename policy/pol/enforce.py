"""Enforcement at the choke points. Every outbound or state-changing path calls Enforcer.check, which evaluates the CURRENT
policy (policy_source is called at every use, never cached), logs the decision with its policy version, and raises
PolicyDenied on anything but an explicit allow.

Labelled data travels as Block(text, label, lineage). Plain str is never accepted at a choke point. derive() is the only
sanctioned way to make a derived block: its label is the join of the inputs'. Python str operations drop labels, which is why
labels live on Block and not on a str subclass.

Wrappers duck-type memcore.ports.ChatModel / Embedder (complete(messages, system=...) / embed(texts, kind)) so they sit in
front of the real adapters; PolicyDenied is deliberately not a PortError, so no failover wrapper may retry it on another model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .labels import Label, join, leq
from .pdp import Decision, Policy, Principal, decide


class PolicyDenied(Exception):
    retryable = False

    def __init__(self, decision: Decision):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class Block:
    text: str
    label: Label
    lineage: Tuple[str, ...] = ()


def derive(inputs: Sequence[Block], text: str, mode: str = "join", lineage_id: Optional[str] = None) -> Block:
    """Derived data inherits the join of its inputs' labels. mode='first' is the broken control (drops the other inputs)."""
    if not inputs:
        raise ValueError("derive needs inputs")
    lab = join(b.label for b in inputs) if mode == "join" else inputs[0].label
    lin = tuple(l for b in inputs for l in b.lineage) + ((lineage_id,) if lineage_id else ())
    return Block(text, lab, lin)


class DecisionLog:
    def __init__(self):
        self.entries: List[Dict[str, Any]] = []

    def add(self, d: Decision, principal_id, label_digest, dest):
        self.entries.append({"seq": len(self.entries), "policy_version": d.policy_version, "action": d.action, "principal": principal_id,
                             "allow": d.allow, "reason": d.reason, "label": label_digest, "dest": dest})


ALLOWED_CTX = frozenset(["destination", "authorization", "recipient_id", "credential", "derived"])


class Enforcer:
    """The only place where trusted facts enter a decision. A request may NAME ids (destination, authorization, recipient_id);
    region, clock, the principal's attributes and the recipient's attributes come from the directory and clock the Enforcer owns.
    Any other context key is a forged context and denies."""

    def __init__(self, policy_source: Callable[[], Policy], log: Optional[DecisionLog] = None, disabled: Iterable[str] = (),
                 directory: Optional[Callable[[str], Optional[Principal]]] = None, clock: Callable[[], float] = None):
        import time as _t
        self.policy_source = policy_source
        self.log = log if log is not None else DecisionLog()
        self.disabled = frozenset(disabled)         # CONTROL ONLY: actions whose enforcement is switched off
        self.evaluated_actions = set()
        self.directory = directory
        self.clock = clock or _t.time

    def check(self, principal, action, labels: Sequence[Label], ctx: Optional[dict] = None, texts: Sequence[str] = (), resolve=None) -> Decision:
        ctx = dict(ctx or {})
        pol = self.policy_source()                  # evaluated at the moment of use
        pid = getattr(principal, "id", None)
        try:
            dig = join(labels).digest()
        except Exception:
            dig = None
        if action in self.disabled:
            d = Decision(True, "ENFORCEMENT_DISABLED", getattr(pol, "version", None), action)
            self.log.add(d, pid, dig, ctx.get("destination"))
            return d
        self.evaluated_actions.add(action)
        res = resolve or self.directory
        d = self._decide(principal, pid, action, labels, ctx, pol, res)
        self.log.add(d, pid, dig, ctx.get("destination"))
        if not d.allow:
            raise PolicyDenied(d)
        return d

    def _decide(self, principal, pid, action, labels, ctx, pol, res):
        ver = getattr(pol, "version", None)
        deny = lambda r: Decision(False, r, ver, action)
        if set(ctx) - ALLOWED_CTX:
            return deny("forged_context")
        if res is not None:
            principal = res(pid)
            if principal is None:
                return deny("unknown_principal")
        trusted = {"now": self.clock()}
        if action == "send_inbox":
            rid = ctx.get("recipient_id")
            if not isinstance(rid, str) or res is None:
                return deny("recipient_required")
            rcp = res(rid)
            if rcp is None:
                return deny("recipient_unknown")
            trusted["recipient"] = rcp
        return decide(principal, action, list(labels), {k: v for k, v in ctx.items() if k != "recipient_id"}, policy=pol, trusted=trusted)


def public_label(tenant: str) -> Label:
    return Label(tenants=frozenset([tenant]))


# ------------------------------------------------------------------------------------------------ guarded ports
class GuardedChat:
    """Front of a ChatModel. The destination id names a policy destination; its pinned model must be what answers."""

    def __init__(self, inner, enforcer: Enforcer, dest_id: str):
        self.inner, self.enf, self.dest_id = inner, enforcer, dest_id

    def complete(self, principal: Principal, blocks: Sequence[Block], system: Sequence[Block] = (), ctx: Optional[dict] = None,
                 derive_mode: str = "join") -> Block:
        allb = list(system) + list(blocks)
        c = dict(ctx or {}, destination=self.dest_id)
        self.enf.check(principal, "send_model", [b.label for b in allb], c, [b.text for b in allb])
        msgs = [{"role": "user", "content": "\n".join(b.text for b in blocks)}]
        res = self.inner.complete(msgs, system="\n".join(b.text for b in system) or None)
        pinned = self.enf.policy_source().destinations[self.dest_id].model
        if getattr(res, "model", None) != pinned:
            d = Decision(False, "pin_drift", self.enf.policy_source().version, "send_model")
            self.enf.log.add(d, principal.id, None, self.dest_id)
            raise PolicyDenied(d)               # the answer is dropped and never becomes a record
        return derive(allb, res.text, derive_mode)


class GuardedEmbedder:
    def __init__(self, inner, enforcer: Enforcer, dest_id: str):
        self.inner, self.enf, self.dest_id = inner, enforcer, dest_id

    def embed(self, principal: Principal, blocks: Sequence[Block], kind: str = "document", ctx: Optional[dict] = None):
        c = dict(ctx or {}, destination=self.dest_id)
        self.enf.check(principal, "embed", [b.label for b in blocks], c, [b.text for b in blocks])
        return self.inner.embed([b.text for b in blocks], kind)


class GuardedExport:
    def __init__(self, sink: Callable[[str, str, str], None], enforcer: Enforcer, dest_id: str):
        self.sink, self.enf, self.dest_id = sink, enforcer, dest_id

    def export(self, principal: Principal, blocks: Sequence[Block], ctx: Optional[dict] = None):
        c = dict(ctx or {}, destination=self.dest_id)
        self.enf.check(principal, "export", [b.label for b in blocks], c, [b.text for b in blocks])
        for b in blocks:
            self.sink(self.dest_id, principal.tenant, b.text)


@dataclass
class Record:
    id: str
    text: str
    label: Label
    lineage: Tuple[str, ...] = ()
    kind: str = "message"
    thread: str = ""


class Store:
    """Labelled record store: append and read are choke points. append refuses a record whose label is less restrictive than
    the join of its lineage (derived data can never be less restricted than its inputs)."""

    def __init__(self, enforcer: Enforcer, check_lineage: bool = True):
        self.enf, self.records, self.check_lineage = enforcer, {}, check_lineage

    def append(self, principal: Principal, rec: Record, ctx: Optional[dict] = None):
        labels = [rec.label]
        if self.check_lineage and rec.lineage:
            src = [self.records[i].label for i in rec.lineage]            # KeyError = unknown lineage = refused below
            if not leq(join(src), rec.label):
                d = Decision(False, "label_below_lineage", self.enf.policy_source().version, "append")
                self.enf.log.add(d, principal.id, rec.label.digest(), None)
                raise PolicyDenied(d)
            labels = labels + src
        self.enf.check(principal, "append", labels, ctx)
        self.records[rec.id] = rec

    def read(self, principal: Principal, rid: str, ctx: Optional[dict] = None) -> Record:
        r = self.records[rid]
        self.enf.check(principal, "read", [r.label], ctx)
        return r

    def retrieve(self, principal: Principal, rids: Iterable[str], ctx: Optional[dict] = None) -> List[Record]:
        out = []
        for rid in rids:
            try:
                out.append(self.read(principal, rid, ctx))
            except PolicyDenied:
                pass
        return out


def inject(enf: Enforcer, principal: Principal, records: Sequence[Record], ctx: Optional[dict] = None) -> Tuple[List[Block], int]:
    """Context assembly: a record the principal may not have in a prompt for this destination is withheld, with an opaque
    placeholder that carries no content, id or count of hidden labels."""
    out, withheld = [], 0
    for r in records:
        try:
            enf.check(principal, "inject", [r.label], ctx)
            out.append(Block(r.text, r.label, (r.id,)))
        except PolicyDenied:
            withheld += 1
    if withheld:
        out.append(Block("[context withheld by policy]", public_label(principal.tenant)))
    return out, withheld


class Outbox:
    """Queued delivery. The decision is taken at enqueue AND again at delivery against the current policy, the current directory entries of
    sender and recipient, and (when `resolver` is given) the CURRENT labels and existence of the source records: a record that was narrowed,
    put under hold or erased after enqueue is re-labelled or refused. enqueue_only=True is the broken control."""

    def __init__(self, enforcer: Enforcer, directory: Callable[[str], Optional[Principal]], action: str,
                 sink: Callable[[Principal, Sequence[Block], dict], None], enqueue_only: bool = False, resolver: Optional[Callable] = None):
        self.enf, self.directory, self.action, self.sink, self.enqueue_only, self.resolver = enforcer, directory, action, sink, enqueue_only, resolver
        self.queue: List[dict] = []
        self.delivered: List[dict] = []
        self.refused: List[dict] = []

    def _labels(self, item):
        labs = [b.label for b in item["blocks"]]
        if self.resolver and item.get("record_ids"):
            for rid in item["record_ids"]:
                r = self.resolver(rid)
                if r is None:
                    d = Decision(False, "source_erased_or_missing", self.enf.policy_source().version, self.action)
                    self.enf.log.add(d, item["sender"], None, None)
                    raise PolicyDenied(d)
                labs.append(r.label)                   # as they are NOW; joined with the queued label (never less restrictive)
        return labs

    def _ctx(self, item):
        c = dict(item["ctx"])
        if item.get("recipient"):
            c["recipient_id"] = item["recipient"]
        return c

    def _check(self, item):
        self.enf.check(self.directory(item["sender"]) if self.directory(item["sender"]) else Principal("?", "?"), self.action, self._labels(item), self._ctx(item),
                       [b.text for b in item["blocks"]], resolve=self.directory)

    def enqueue(self, sender_id: str, blocks: Sequence[Block], ctx: dict, recipient_id: Optional[str] = None, record_ids: Optional[Sequence[str]] = None):
        item = {"sender": sender_id, "blocks": list(blocks), "ctx": dict(ctx), "recipient": recipient_id, "record_ids": list(record_ids or [])}
        self._check(item)
        item["enqueue_version"] = self.enf.policy_source().version
        self.queue.append(item)

    def deliver_all(self):
        q, self.queue = self.queue, []
        for item in q:
            try:
                if not self.enqueue_only:
                    self._check(item)
                self.sink(self.directory(item["sender"]), item["blocks"], dict(self._ctx(item), recipient=self.directory(item["recipient"]) if item.get("recipient") else None))
                self.delivered.append(item)
            except PolicyDenied as e:
                item["refusal"] = e.decision
                self.refused.append(item)
        return self.delivered, self.refused
