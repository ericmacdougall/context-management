"""Random conversations with tool use for both shapes. Seeded; deterministic."""
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


def abstract(rng: random.Random, max_turns=9):
    """List of steps. ("user", text) | ("tools", assistant_text, [(id,name,args)...], [result_text...], trailing_text_or_None)."""
    steps, n = [], 0
    for t in range(rng.randint(1, max_turns)):
        steps.append(("user", text(rng)))
        for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
            k = rng.choice([1, 1, 1, 2, 3])
            calls = []
            for _ in range(k):
                n += 1
                calls.append(("call_%d_%x" % (n, rng.getrandbits(24)), rng.choice(["recall", "open_event", "timeline", "bash"]), {"q": text(rng, 1, 6)}))
            steps.append(("tools", text(rng, 0, 12) if rng.random() < 0.6 else "", calls, [blob(rng) for _ in calls], text(rng, 1, 8) if rng.random() < 0.3 else None))
        if t < 8 and rng.random() < 0.8:
            steps.append(("assistant", text(rng, 1, 40)))
    return steps


def render(shape: str, steps, rng: random.Random, system: bool = True) -> List[dict]:
    msgs: List[dict] = []
    if shape == "openai" and system:
        msgs.append({"role": "system", "content": "You are a helpful assistant. " + text(rng, 3, 20)})
    for s in steps:
        if s[0] == "user":
            msgs.append({"role": "user", "content": s[1]})
        elif s[0] == "assistant":
            msgs.append({"role": "assistant", "content": s[1]} if shape == "openai" else {"role": "assistant", "content": [{"type": "text", "text": s[1]}]})
        else:
            _, atext, calls, results, trailing = s
            order = list(range(len(calls)))
            rng.shuffle(order)
            if shape == "anthropic":
                bl = ([{"type": "text", "text": atext}] if atext else []) + [{"type": "tool_use", "id": c[0], "name": c[1], "input": c[2]} for c in calls]
                msgs.append({"role": "assistant", "content": bl})
                rb = [{"type": "tool_result", "tool_use_id": calls[i][0], "content": results[i] or "(no output)"} for i in order]
                if trailing:
                    rb.append({"type": "text", "text": trailing})
                msgs.append({"role": "user", "content": rb})
            else:
                msgs.append({"role": "assistant", "content": atext or None, "tool_calls": [
                    {"id": c[0], "type": "function", "function": {"name": c[1], "arguments": json.dumps(c[2])}} for c in calls]})
                for i in order:
                    msgs.append({"role": "tool", "tool_call_id": calls[i][0], "content": results[i]})
                if trailing:
                    msgs.append({"role": "user", "content": trailing})
    # a conversation sent to the model must end with user (or tool for openai): drop a trailing assistant text message
    while msgs and msgs[-1]["role"] == "assistant":
        msgs.pop()
    return msgs


def conversation(rng: random.Random, shape: str) -> List[dict]:
    while True:
        m = render(shape, abstract(rng), rng)
        core = [x for x in m if x["role"] != "system"]
        if core and core[0]["role"] == "user":
            return m
