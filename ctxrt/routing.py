"""P6: reviewer routing. The author of a finding or proposal, its id aliases (same lineage) and its delegates can never review or approve it; the
author id and author model are recorded on every record; the reviewer/click-tester inbox never claims author-origin items."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from memcore.inbox import InboxService, SessionRecord, InboxItem


class RoutingRefused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Agent:
    agent_id: str
    model_id: str
    lineage: str                       # restarts and forks of one logical agent share a lineage
    delegated_by: Optional[str] = None
    available: bool = True
    role: str = "worker"               # worker | reviewer | click_tester | analyst


class Registry:
    def __init__(self, agents: List[Agent]):
        self.agents = {a.agent_id: a for a in agents}

    def author_set(self, author_id: str) -> Set[str]:
        """Author, every agent of its lineage (aliases), and every delegate (transitively) of any of those."""
        a = self.agents[author_id]
        s = {x.agent_id for x in self.agents.values() if x.lineage == a.lineage}
        changed = True
        while changed:
            changed = False
            for x in self.agents.values():
                if x.agent_id not in s and x.delegated_by in s:
                    s.add(x.agent_id)
                    changed = True
            lin = {self.agents[i].lineage for i in s}          # a delegate's own aliases are delegates too
            for x in self.agents.values():
                if x.agent_id not in s and x.lineage in lin:
                    s.add(x.agent_id)
                    changed = True
        return s


class RecordStore:
    KINDS = ("finding", "proposal")

    def __init__(self, registry: Registry):
        self.reg, self.records, self._n = registry, {}, itertools.count(1)

    def create(self, kind: str, author_id: Optional[str], author_model: Optional[str], body: str) -> dict:
        if kind not in self.KINDS:
            raise RoutingRefused("bad_kind")
        if not author_id or not author_model or author_id not in self.reg.agents:
            raise RoutingRefused("author_and_model_required")
        if self.reg.agents[author_id].model_id != author_model:
            raise RoutingRefused("author_model_mismatch")
        rid = "r%d" % next(self._n)
        self.records[rid] = {"rid": rid, "kind": kind, "author_id": author_id, "author_model": author_model, "body": body, "approved_by": None, "reviewer": None}
        return self.records[rid]

    def route_review(self, rid: str, pool: List[str], require_different_model: bool = True) -> str:
        r = self.records[rid]
        bad = self.reg.author_set(r["author_id"])
        elig = sorted(i for i in pool if i in self.reg.agents and i not in bad and self.reg.agents[i].available
                      and self.reg.agents[i].role in ("worker", "reviewer", "click_tester")
                      and (not require_different_model or self.reg.agents[i].model_id != r["author_model"]))
        if not elig:
            raise RoutingRefused("no_eligible_reviewer")
        r["reviewer"] = elig[0]
        return elig[0]

    def approve(self, rid: str, approver_id: str) -> None:
        r = self.records[rid]
        if approver_id in self.reg.author_set(r["author_id"]):
            raise RoutingRefused("author_cannot_approve")
        if r["reviewer"] is not None and approver_id != r["reviewer"]:
            raise RoutingRefused("not_the_routed_reviewer")
        r["approved_by"] = approver_id


def route_by_availability(reg: Registry, record: dict, pool: List[str]) -> Optional[str]:
    """CONTROL: first available agent in the pool, author or not."""
    for i in sorted(pool):
        if reg.agents[i].available:
            return i
    return None


class FilteredInbox(InboxService):
    """Reviewer / click-tester inbox: an item whose origin agent is in the recipient's author set is never eligible. Origin per finding_id is
    kept in `origins` (finding_id -> author agent id); sessions map to agents in `session_agent`."""

    def __init__(self, *a, registry: Registry, origins: Dict[str, str], session_agent: Dict[str, str], **k):
        super().__init__(*a, **k)
        self.reg, self.origins, self.session_agent = registry, origins, session_agent

    def _eligible(self, s: SessionRecord, it: InboxItem, now: float) -> bool:
        if not super()._eligible(s, it, now):
            return False
        recipient = self.session_agent.get(s.session_id)
        origin = self.origins.get(it.finding_id)
        if recipient is not None and origin is not None and recipient in self.reg.author_set(origin):
            return False
        return True
