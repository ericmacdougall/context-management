"""The four memory tools described once as data, with handlers over a MemoryStore protocol.
InMemoryStore is the fake used until the real store exists. Timestamps are ISO-8601 UTC strings of one format
(compared lexicographically)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .fakes import tokens
from .ports import AgentToolSurface, ContextInjector, Injection, ToolSpec, Turn


class InMemoryStore:
    def __init__(self):
        self.events: Dict[str, dict] = {}
        self.facts: List[dict] = []

    def add_event(self, id: str, session: str, ts: str, text: str, entities: Optional[List[str]] = None) -> None:
        self.events[id] = {"id": id, "session": session, "ts": ts, "text": text, "entities": entities or []}

    def add_fact(self, id: str, subject: str, predicate: str, value: str, valid_from: str, valid_to: Optional[str], event_id: str) -> None:
        self.facts.append({"id": id, "subject": subject, "predicate": predicate, "value": value,
                           "valid_from": valid_from, "valid_to": valid_to, "event_id": event_id})

    # tool backends
    def recall(self, query: str, as_of: Optional[str] = None, k: int = 5) -> List[dict]:
        q = set(tokens(query))
        sc = []
        for e in self.events.values():
            if as_of and e["ts"] > as_of:
                continue
            s = len(q & set(tokens(e["text"])))
            if s:
                sc.append((-s, e["ts"], e["id"], e))
        return [{"id": x[3]["id"], "ts": x[3]["ts"], "text": x[3]["text"], "score": -x[0]} for x in sorted(sc, key=lambda t: t[:3])[:k]]

    def open_event(self, id: str) -> Optional[dict]:
        return self.events.get(id)

    def query_facts(self, subject: str, predicate: Optional[str] = None, as_of: Optional[str] = None) -> List[dict]:
        out = []
        for f in self.facts:
            if f["subject"].lower() != subject.lower() or (predicate and f["predicate"] != predicate):
                continue
            if as_of and not (f["valid_from"] <= as_of and (f["valid_to"] is None or as_of < f["valid_to"])):
                continue
            out.append(f)
        return sorted(out, key=lambda f: f["valid_from"])

    def timeline(self, entity: str) -> List[dict]:
        ev = [e for e in self.events.values() if entity.lower() in [x.lower() for x in e["entities"]] or entity.lower() in e["text"].lower()]
        return [{"id": e["id"], "ts": e["ts"], "text": e["text"]} for e in sorted(ev, key=lambda e: (e["ts"], e["id"]))]


_S = {"type": "string"}
_ASOF = {"type": "string", "description": "ISO-8601 UTC time; answer as the world was at this time"}


def memory_tools(store) -> AgentToolSurface:
    def obj(props, req):
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}
    return AgentToolSurface([
        ToolSpec("recall", "Search remembered events by text; optionally as of a past time.",
                 obj({"query": _S, "as_of": _ASOF, "k": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]),
                 lambda a: store.recall(a["query"], a.get("as_of"), a.get("k", 5))),
        ToolSpec("open_event", "Open one remembered event by id, with its full text.",
                 obj({"id": _S}, ["id"]), lambda a: store.open_event(a["id"])),
        ToolSpec("query_facts", "List facts about a subject (optionally one predicate) valid at a time.",
                 obj({"subject": _S, "predicate": _S, "as_of": _ASOF}, ["subject"]),
                 lambda a: store.query_facts(a["subject"], a.get("predicate"), a.get("as_of"))),
        ToolSpec("timeline", "Chronological events that mention an entity.",
                 obj({"entity": _S}, ["entity"]), lambda a: store.timeline(a["entity"])),
    ])


class BudgetedInjector(ContextInjector):
    """Recall on the user's text and render at most `char_budget` characters; only ids are returned for logging."""

    def __init__(self, store, char_budget: int = 800, k: int = 5):
        self.store, self.char_budget, self.k = store, char_budget, k

    def build(self, turn: Turn) -> Injection:
        lines, used, n = [], [], 0
        for h in self.store.recall(turn.user_text, as_of=turn.time, k=self.k):
            line = "- [%s %s] %s" % (h["id"], h["ts"], h["text"])
            if n + len(line) > self.char_budget:
                break
            lines.append(line)
            used.append(h["id"])
            n += len(line) + 1
        block = "<memory>\n" + "\n".join(lines) + "\n</memory>" if lines else ""
        return Injection(block=block, used_ids=used)
