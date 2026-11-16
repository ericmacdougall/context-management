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


class Enforcer:
    def __init__(self, policy_source: Callable[[], Policy], log: Optional[DecisionLog] = None, disabled: Iterable[str] = ()):
        self.policy_source = policy_source
        self.log = log if log is not None else DecisionLog()
        self.disabled = frozenset(disabled)         # CONTROL ONLY: actions whose enforcement is switched off
        self.evaluated_actions = set()

    def check(self, principal, action, labels: Sequence[Label], ctx: Optional[dict] = None) -> Decision:
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
        d = decide(principal, action, list(labels), ctx, policy=pol)
        self.log.add(d, pid, dig, ctx.get("destination"))
        if not d.allow:
            raise PolicyDenied(d)
        return d


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
        self.enf.check(principal, "send_model", [b.label for b in allb], c)
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
        self.enf.check(principal, "embed", [b.label for b in blocks], c)
        return self.inner.embed([b.text for b in blocks], kind)


class GuardedExport:
    def __init__(self, sink: Callable[[str, str, str], None], enforcer: Enforcer, dest_id: str):
        self.sink, self.enf, self.dest_id = sink, enforcer, dest_id

    def export(self, principal: Principal, blocks: Sequence[Block], ctx: Optional[dict] = None):
        c = dict(ctx or {}, destination=self.dest_id)
        self.enf.check(principal, "export", [b.label for b in blocks], c)
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
    """Queued delivery. The decision is taken at enqueue AND again at delivery against the current policy and the current
    directory entry of the sender and recipient. enqueue_only=True is the broken control (the old check is trusted)."""

    def __init__(self, enforcer: Enforcer, directory: Callable[[str], Optional[Principal]], action: str,
                 sink: Callable[[Principal, Sequence[Block], dict], None], enqueue_only: bool = False):
        self.enf, self.directory, self.action, self.sink, self.enqueue_only = enforcer, directory, action, sink, enqueue_only
        self.queue: List[dict] = []
        self.delivered: List[dict] = []
        self.refused: List[dict] = []

    def _ctx(self, item):
        c = dict(item["ctx"])
        if item.get("recipient"):
            c["recipient"] = self.directory(item["recipient"])
        return c

    def enqueue(self, sender_id: str, blocks: Sequence[Block], ctx: dict, recipient_id: Optional[str] = None):
        p = self.directory(sender_id)
        item = {"sender": sender_id, "blocks": list(blocks), "ctx": dict(ctx), "recipient": recipient_id}
        self.enf.check(p, self.action, [b.label for b in blocks], self._ctx(item))
        item["enqueue_version"] = self.enf.policy_source().version
        self.queue.append(item)

    def deliver_all(self):
        q, self.queue = self.queue, []
        for item in q:
            p = self.directory(item["sender"])
            try:
                if not self.enqueue_only:
                    self.enf.check(p, self.action, [b.label for b in item["blocks"]], self._ctx(item))
                self.sink(p, item["blocks"], self._ctx(item))
                self.delivered.append(item)
            except PolicyDenied as e:
                item["refusal"] = e.decision
                self.refused.append(item)
        return self.delivered, self.refused
