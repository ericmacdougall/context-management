"""Random conversations with tool use for both shapes. Seeded; deterministic. Covers what current providers send: thinking blocks with signatures and
redacted thinking, server-tool blocks (resolved, and one left unresolved at the tail), images and documents in human messages, tool errors, empty tool
results (no content key), list-valued tool results, parallel tool calls with results in shuffled order, CJK and emoji text."""
from __future__ import annotations

import json
import random
from typing import List

WORDS = "alpha beta gamma delta staging database Dublin Priya invoice retry cache deploy rollback quarter café über 日本語 emoji\U0001F600".split()


def text(rng: random.Random, lo=1, hi=40) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(lo, hi)))


def blob(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.08:
        return ""
    if r < 0.18:
        return text(rng, 800, 3000)
    return text(rng, 1, 60)


def sig(rng):
    return "sig-%x" % rng.getrandbits(64)


def abstract(rng: random.Random, max_turns=9):
    steps, n = [], 0
    for t in range(rng.randint(1, max_turns)):
        steps.append(("user", text(rng), rng.choice([None, None, None, "image", "document"])))
        for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
            k = rng.choice([1, 1, 1, 2, 3])
            calls = []
            for _ in range(k):
                n += 1
                calls.append(("call_%d_%x" % (n, rng.getrandbits(24)), rng.choice(["recall", "open_event", "timeline", "bash"]), {"q": text(rng, 1, 6)}))
            results = [(blob(rng), rng.random() < 0.15, rng.random() < 0.3) for _ in calls]            # (text, is_error, as_list)
            steps.append(("tools", text(rng, 0, 12) if rng.random() < 0.6 else "", calls, results, text(rng, 1, 8) if rng.random() < 0.3 else None,
                          rng.random() < 0.5, rng.random() < 0.15, rng.random() < 0.2))            # thinking, redacted, server tool (resolved)
        if t < 8 and rng.random() < 0.8:
            steps.append(("assistant", text(rng, 1, 40), rng.random() < 0.3))
    return steps


def render(shape: str, steps, rng: random.Random, system: bool = True) -> List[dict]:
    msgs: List[dict] = []
    if shape == "openai" and system:
        msgs.append({"role": "system", "content": "You are a helpful assistant. " + text(rng, 3, 20)})
    for s in steps:
        if s[0] == "user":
            if shape == "anthropic" and s[2]:
                media = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}} if s[2] == "image" else \
                    {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "doc text"}}
                msgs.append({"role": "user", "content": [media, {"type": "text", "text": s[1]}]})
            else:
                msgs.append({"role": "user", "content": s[1]})
        elif s[0] == "assistant":
            if shape == "openai":
                msgs.append({"role": "assistant", "content": s[1]})
            else:
                bl = ([{"type": "thinking", "thinking": text(rng, 1, 10), "signature": sig(rng)}] if s[2] else []) + [{"type": "text", "text": s[1]}]
                msgs.append({"role": "assistant", "content": bl})
        else:
            _, atext, calls, results, trailing, think, redact, srv = s
            order = list(range(len(calls)))
            rng.shuffle(order)
            if shape == "anthropic":
                bl = []
                if think:
                    bl.append({"type": "thinking", "thinking": text(rng, 1, 10), "signature": sig(rng)})
                if redact:
                    bl.append({"type": "redacted_thinking", "data": "enc-%x" % rng.getrandbits(48)})
                if srv:
                    sid = "srvtoolu_%x" % rng.getrandbits(24)
                    bl += [{"type": "server_tool_use", "id": sid, "name": "web_search", "input": {"query": "x"}},
                           {"type": "web_search_tool_result", "tool_use_id": sid, "content": [{"type": "web_search_result", "url": "https://example.com", "title": "t"}]}]
                if atext:
                    bl.append({"type": "text", "text": atext})
                bl += [{"type": "tool_use", "id": c[0], "name": c[1], "input": c[2]} for c in calls]
                msgs.append({"role": "assistant", "content": bl})
                rb = []
                for i in order:
                    r = {"type": "tool_result", "tool_use_id": calls[i][0]}
                    txt, err, as_list = results[i]
                    if txt:
                        r["content"] = [{"type": "text", "text": txt}] if as_list else txt
                    if err:
                        r["is_error"] = True
                    rb.append(r)
                if trailing:
                    rb.append({"type": "text", "text": trailing})
                msgs.append({"role": "user", "content": rb})
            else:
                msgs.append({"role": "assistant", "content": atext or None, "tool_calls": [
                    {"id": c[0], "type": "function", "function": {"name": c[1], "arguments": json.dumps(c[2])}} for c in calls]})
                for i in order:
                    msgs.append({"role": "tool", "tool_call_id": calls[i][0], "content": results[i][0] or ""})
                if trailing:
                    msgs.append({"role": "user", "content": trailing})
    while msgs and msgs[-1]["role"] == "assistant":
        msgs.pop()
    return msgs


def conversation(rng: random.Random, shape: str) -> List[dict]:
    while True:
        m = render(shape, abstract(rng), rng)
        core = [x for x in m if x["role"] != "system"]
        if core and core[0]["role"] == "user":
            return m


def with_unresolved_server_tool(rng: random.Random, conv: List[dict]) -> List[dict]:
    """Anthropic only: replace the tail by an assistant turn that called a client tool AND a server tool with no result yet (pause), followed by the
    user message holding ONLY the client tool_result. Returns a new conversation (the hard injection case)."""
    out = [m for m in conv]
    # cut at the last human message
    k = max(i for i, m in enumerate(out) if m["role"] == "user" and not (isinstance(m["content"], list) and m["content"] and m["content"][0].get("type") == "tool_result"))
    out = out[:k + 1]
    cid, sid = "call_u_%x" % rng.getrandbits(24), "srvtoolu_u_%x" % rng.getrandbits(24)
    out.append({"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": sig(rng)}, {"type": "server_tool_use", "id": sid, "name": "web_search", "input": {"query": "x"}},
                                                  {"type": "tool_use", "id": cid, "name": "recall", "input": {"q": "x"}}]})
    out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "ok"}]})
    return out
