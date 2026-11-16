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
    for i, k in enumerate(plan):
        if i > 0 and plan[i - 1] != "text":                     # answer the previous tool call with a tool_result / tool message
            if provider == "anthropic":
                rig.msgs.append({"role": "user", "content": [tool_result("tu_%d" % (i - 1), "result %d" % (i - 1)), text("continue %d" % i)]})
            else:
                rig.msgs.append({"role": "tool", "tool_call_id": "call_%d" % (i - 1), "content": "result %d" % (i - 1)})
            o = rig.call(rig.request())
            rig.msgs.append({"role": "assistant", "content": o["content"]} if provider == "anthropic" else o["choices"][0]["message"])
        else:
            rig.ask("question %d" % i)
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
