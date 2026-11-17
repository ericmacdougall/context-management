"""Shared helpers for the proof runs: artifact stamping, conversation generator, log copying."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*a: str) -> str:
    return subprocess.run(["git", "-C", REPO] + list(a), capture_output=True, text=True).stdout.strip()


def stamp(item: str, params: dict) -> dict:
    dirty = git("status", "--porcelain")
    return {"item": item, "commit": git("rev-parse", "HEAD"), "tree_clean": dirty == "", "dirty": dirty[:200],
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": params,
            "env": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
                    "synchronous": "FULL", "journal": "WAL", "load1": os.getloadavg()[0]}}


def write_artifact(out: str, item: str, params: dict, runs: list, summary: dict) -> str:
    art = {"header": stamp(item, params), "runs": runs, "summary": summary}
    body = json.dumps(art, sort_keys=True, indent=1)
    sha = hashlib.sha256(body.encode()).hexdigest()
    p = os.path.join(out, "%s-%s.json" % (item, sha[:12]))
    for old in os.listdir(out):
        if old.startswith(item + "-") and old.endswith(".json"):
            os.remove(os.path.join(out, old))
    with open(p, "w") as f:
        f.write(body)
    return p


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="ingproof-", dir=os.environ.get("INGEST_TMP"))


def scripted_conversation(rig_cls, tmp: str, n_calls: int, seed: int = 0, provider: str = "anthropic", stream: bool = False, **kw):
    """A conversation with text, thinking, tool_use / tool_result turns, generated in memory. Returns the rig after n_calls calls."""
    from .stubs import text, tool_use, tool_result, thinking
    rnd = random.Random(seed)
    script, plan = [], []
    for i in range(n_calls):
        kind = rnd.choice(["text", "tool", "think_tool"]) if i < n_calls - 1 else "text"
        plan.append(kind)
        if provider == "anthropic":
            if kind == "text":
                script.append({"content": [text("answer %d seed %d" % (i, seed))]})
            elif kind == "tool":
                script.append({"content": [text("let me check"), tool_use("tu_%d" % i, "lookup", {"q": i})], "stop": "tool_use"})
            else:
                script.append({"content": [thinking("reasoning %d" % i, "SIGNATURE%d==" % i), tool_use("tu_%d" % i, "lookup", {"q": i})], "stop": "tool_use"})
        else:
            if kind == "text":
                script.append({"message": {"role": "assistant", "content": "answer %d seed %d" % (i, seed)}})
            else:
                script.append({"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_%d" % i, "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"q": i})}}]}, "stop": "tool_calls"})
    rig = rig_cls(tmp, provider, script, stream=stream, **kw)
    led = HostLedger(provider)
    for i, k in enumerate(plan):
        c = led.begin_call()
        if i > 0 and plan[i - 1] != "text":                     # answer the previous tool call with a tool_result / tool message
            if provider == "anthropic":
                m = {"role": "user", "content": [tool_result("tu_%d" % (i - 1), "result %d" % (i - 1)), text("continue %d" % i)]}
            else:
                m = {"role": "tool", "tool_call_id": "call_%d" % (i - 1), "content": "result %d" % (i - 1)}
            rig.msgs.append(m)
            led.new_request_msg(c, m)
            o = rig.call(rig.request())
            rig.msgs.append({"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"])
        else:
            m = {"role": "user", "content": "question %d" % i}
            rig.msgs.append(m)
            led.new_request_msg(c, m)
            o = rig.call(rig.request())
            rig.msgs.append({"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"])
        led.response(c, rig.msgs[-1])
    rig.ledger = led
    return rig


def copy_log_prefix(src_path: str, dst_path: str, upto_seq: int) -> None:
    """Build a truncated copy of a port log (rows with seq <= upto_seq and all blobs): models a truncated log file."""
    from .portlog import PortLog
    d = PortLog(dst_path)
    s = sqlite3.connect(src_path)
    c = d.conn()
    for row in s.execute("SELECT stream,seq,kind,call_n,body,prev,hash,ts FROM port_log WHERE seq<=? ORDER BY seq", (upto_seq,)):
        c.execute("INSERT INTO port_log VALUES(?,?,?,?,?,?,?,?)", row)
    for row in s.execute("SELECT h, body FROM blobs"):
        c.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", row)
    s.close()
    d.close()


def event_sig(events: List[dict]) -> List[list]:
    import hashlib as h
    return [[e["ikey"], h.sha256((e["content"] or "").encode()).hexdigest()[:16], e["status"]] for e in events]


# ---------------------------------------------------------------- provider-neutral spec/message builders
def spec_text(provider: str, t: str, **kw) -> dict:
    from .stubs import text
    return ({"content": [text(t)], **kw} if provider == "anthropic" else {"message": {"role": "assistant", "content": t}, **kw})


def asst_msg(provider: str, o: dict) -> dict:
    return {"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"]


def resp_text(provider: str, o: dict) -> str:
    return o["content"][0]["text"] if provider == "anthropic" else o["choices"][0]["message"]["content"]


# ---------------------------------------------------------------- independent expected-event oracle (PREREG-amend-3 A4)
# Built at the HOST while a scripted conversation runs, from the messages the host appends and the response the stub returns.
# It imports no Ingester, reads no log record and no event key, and does not call shapes.blocks_of_message: provider message
# structure is re-derived here on purpose, so a shared omission in the ingest path cannot also be in the oracle.
def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_text(c) -> str:
    """Tool-result content as text: a string, or the concatenated text of a list of text blocks."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return "".join(x.get("text", "") for x in c if isinstance(x, dict))


def msg_tuples(provider: str, msg: dict, call_n: int, side: str) -> List[tuple]:
    """(call_n, side, prov, kind, ident, payload). payload: tool name and canonical arguments for a tool call, the result text for a tool
    result, the signature for thinking (PREREG-amend-4 A2); empty for text, where ident is the text."""
    out: List[tuple] = []
    role, c = msg.get("role"), msg.get("content")
    if provider == "anthropic":
        blocks = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
        for b in blocks:
            t = b.get("type")
            if t == "text":
                out.append((call_n, side, role, "text", b["text"], ""))
            elif t == "thinking":
                out.append((call_n, side, "assistant_thinking", "thinking", b["thinking"], b.get("signature", "")))
            elif t == "tool_use":
                out.append((call_n, side, "assistant", "tool_call", b["id"], b["name"] + "|" + _canon(b.get("input"))))
            elif t == "tool_result":
                out.append((call_n, side, "tool", "tool_result", b["tool_use_id"], _content_text(b.get("content"))))
        return out
    if role == "tool":
        return [(call_n, side, "tool", "tool_result", msg["tool_call_id"], _content_text(c))]
    if isinstance(c, str) and c != "":
        out.append((call_n, side, role, "text", c, ""))
    for tc in msg.get("tool_calls") or []:
        fn = tc["function"]
        out.append((call_n, side, "assistant", "tool_call", tc["id"], fn["name"] + "|" + _canon(json.loads(fn["arguments"]))))
    return out


class HostLedger:
    """Expected events, recorded by the host: request side = messages the host newly appended before a call; response side = the
    message the stub returned. Retries and flagged duplicates are declared by the scenario, not inferred."""

    def __init__(self, provider: str):
        self.provider, self.rows, self.calls = provider, [], 0

    def begin_call(self) -> int:
        self.calls += 1
        return self.calls

    def new_request_msg(self, call_n: int, msg: dict) -> None:
        self.rows += msg_tuples(self.provider, msg, call_n, "req")

    def response(self, call_n: int, msg: dict) -> None:
        self.rows += msg_tuples(self.provider, msg, call_n, "resp")

    def expected(self) -> List[list]:
        return sorted([list(r) for r in self.rows], key=lambda r: (r[0], r[1], r[2], r[3], str(r[4]), str(r[5])))


def event_tuples(events: List[dict]) -> List[list]:
    """Project ingested events to the oracle's tuple shape, payload included (port, injected-block and host-authored events excluded)."""
    out = []
    for e in events:
        if e["prov"] in ("port", "memory_injected", "host"):
            continue
        data = json.loads(e["content"]) if e["content"] else {}
        meta = json.loads(e["meta"]) if e["meta"] else {}
        payload = ""
        if e["kind"] == "text":
            ident = data.get("text")
        elif e["kind"] == "thinking":
            ident, payload = data.get("thinking"), data.get("signature", "")
        elif e["kind"] == "tool_call":
            ident = meta.get("tool_id")
            if "function" in data:
                payload = data["function"]["name"] + "|" + _canon(json.loads(data["function"]["arguments"]))
            else:
                payload = data.get("name", "") + "|" + _canon(data.get("input"))
        elif e["kind"] == "tool_result":
            ident = meta.get("tool_id")
            payload = _content_text(data.get("content")) if "content" in data else data.get("text", "")
        else:
            ident = meta.get("tool_id")
        out.append([e["call_n"], e["side"], e["prov"], e["kind"], ident, payload])
    return sorted(out, key=lambda r: (r[0], r[1], r[2], r[3], str(r[4]), str(r[5])))


def legacy_event_tuples(events: List[dict]) -> List[list]:
    """The id-only projection used up to add7c86 (kept to show what it cannot see)."""
    return sorted([r[:5] for r in event_tuples(events)], key=lambda r: (r[0], r[1], r[2], r[3], str(r[4])))


def repeated_conversation(rig_cls, tmp: str, provider: str, pattern: str, window: int, n: int, response: str = "ok", per_call: int = 1,
                          boundary: bool = False, **kw):
    """A host that sends repeated or alternating user text and keeps only the last `window` messages, appending `per_call` new user
    messages before each call. With boundary=True it declares new_count=per_call. Returns (rig, ledger)."""
    from .stubs import text
    script = [{"content": [text(response)]} if provider == "anthropic" else {"message": {"role": "assistant", "content": response}} for _ in range(n)]
    rig = rig_cls(tmp, provider, script, **kw)
    led = HostLedger(provider)
    cycle = {"same": ["yes"], "alt": ["yes", "no"], "tri": ["a", "b", "c"], "unique": None}[pattern]
    for i in range(n):
        c = led.begin_call()
        for k in range(per_call):
            idx = i * per_call + k
            um = {"role": "user", "content": ("q%d" % idx) if cycle is None else cycle[idx % len(cycle)]}
            rig.msgs.append(um)
            led.new_request_msg(c, um)
        o = rig.call(rig.request(), **({"new_count": per_call} if boundary else {}))
        am = {"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"]
        led.response(c, am)
        rig.msgs.append(am)
        while window and len(rig.msgs) > window:
            rig.msgs.pop(0)
    return rig, led
