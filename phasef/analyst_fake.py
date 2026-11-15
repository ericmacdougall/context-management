"""Small fake tool backend for dry runs: lexical search over the 'haystack' list of a fake task file (NOT the real retriever)."""
import json
import re

_STOP = set("a an the of to is are was were be in at on for and or my i me you your do does did what which who how when where it that this with from by as".split())


def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", str(s).lower()) if t not in _STOP and len(t) > 1]


class FakeBackend:
    def __init__(self, task_path):
        self.rounds = json.load(open(task_path))["haystack"]
        self.by_id = {r["round_id"]: r for r in self.rounds}

    def __call__(self, req):
        tool, a = req["tool"], req.get("args", {})
        if tool == "search":
            q = set(toks(a.get("query", "")))
            sc = sorted(((len(q & set(toks(r["text"]))), r) for r in self.rounds), key=lambda x: (-x[0], x[1]["round_id"]))
            return {"results": [{"round_id": r["round_id"], "session_id": r["session_id"], "date": r["date"], "snippet": r["text"][:300]} for s, r in sc[: int(a.get("k", 10))] if s > 0]}
        if tool == "open_round":
            r = self.by_id.get(a.get("round_id"))
            return {"round_id": r["round_id"], "session_id": r["session_id"], "date": r["date"], "text": r["text"]} if r else {"error": "unknown round id"}
        if tool == "open_session":
            rs = [r for r in self.rounds if r["session_id"] == a.get("session_id")]
            return {"session_id": a.get("session_id"), "date": rs[0]["date"] if rs else None, "rounds": [{"round_id": r["round_id"], "snippet": r["text"][:200]} for r in rs]}
        if tool == "timeline":
            term = set(toks(a.get("term", "")))
            seen, out = set(), []
            for r in sorted(self.rounds, key=lambda r: (r["date"], r["round_id"])):
                if r["session_id"] in seen or (term and not term & set(toks(r["text"]))):
                    continue
                seen.add(r["session_id"])
                out.append({"session_id": r["session_id"], "date": r["date"], "first_words": r["text"][:80]})
            return {"sessions": out[:60]}
        return {"error": "unknown tool"}
