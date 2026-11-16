"""P1: provider-valid conversations. Validators for the two message shapes, a pair-preserving trimmer, a placement-aware injector, and
the broken variants used as controls. Pure functions, no I/O.

Anthropic Messages shape: {"role": "user"|"assistant", "content": str | [blocks]}; blocks: text, tool_use{id,name,input},
tool_result{tool_use_id,content}. Rules enforced (see THRESHOLDS.md Amendment 1 for sources and confidence):
  A1 first message is a user message; roles are user or assistant; content is non-empty.
  A2 every assistant message with tool_use blocks is IMMEDIATELY followed by a user message whose content is a list that STARTS with
     exactly one tool_result per tool_use id of that assistant message (any order, no extras, no repeats).
  A3 a tool_result may appear only in such a following user message and only before any non-tool_result block ("results first, text after").
  A4 tool_use ids are unique in the conversation; tool_use only in assistant, tool_result only in user.
  A5 the conversation sent to the model ends with a user message (no pending tool_use, no assistant prefill).
OpenAI-compatible shape: roles system|developer|user|assistant|tool.
  O1 system/developer messages only at the head; O2 an assistant message with tool_calls is immediately followed by one `tool` message per
  call id (any order) before any other role; O3 a tool message must belong to the group right after an assistant tool_calls message (no orphan,
  no unknown or repeated id); O4 call ids unique; O5 the conversation ends with a user or tool message.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple

Msg = Dict[str, Any]
MARKER_TEXT = "[earlier conversation trimmed to fit the context budget]"


# ------------------------------------------------------------------ token estimate (deterministic, provider independent)
def est_tokens(msg: Msg) -> int:
    return max(1, (len(json.dumps(msg, ensure_ascii=False, separators=(",", ":"))) + 3) // 4)


def total_tokens(msgs: List[Msg]) -> int:
    return sum(est_tokens(m) for m in msgs)


# ------------------------------------------------------------------ validators: return a list of violation class names
def _blocks(m: Msg) -> List[dict]:
    c = m.get("content")
    return c if isinstance(c, list) else []


def validate_anthropic(msgs: List[Msg]) -> List[str]:
    v: List[str] = []
    if not msgs:
        return ["empty"]
    if msgs[0].get("role") != "user":
        v.append("head_not_user")
    seen_use = set()
    for i, m in enumerate(msgs):
        role, c = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            v.append("bad_role")
            continue
        if c in ("", None, []):
            v.append("empty_content")
        bl = _blocks(m)
        for b in bl:
            if not isinstance(b, dict) or b.get("type") not in ("text", "tool_use", "tool_result"):
                v.append("bad_block")
            elif b["type"] == "text" and not b.get("text"):
                v.append("empty_text")
            elif b["type"] == "tool_use":
                if role != "assistant":
                    v.append("tool_use_in_user")
                if b.get("id") in seen_use:
                    v.append("duplicate_tool_use_id")
                seen_use.add(b.get("id"))
            elif b["type"] == "tool_result" and role != "user":
                v.append("tool_result_in_assistant")
        uses = [b["id"] for b in bl if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b.get("tool_use_id") for b in bl if isinstance(b, dict) and b.get("type") == "tool_result"]
        prev_uses = []
        if i > 0 and msgs[i - 1].get("role") == "assistant":
            prev_uses = [b["id"] for b in _blocks(msgs[i - 1]) if isinstance(b, dict) and b.get("type") == "tool_use"]
        if results:
            if role == "user":
                prefix = 0
                for b in bl:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        prefix += 1
                    else:
                        break
                if prefix != len(results):
                    v.append("text_before_result")
            if not prev_uses:
                v.append("orphan_result_or_split_pair")
            else:
                if sorted(map(str, results)) != sorted(map(str, prev_uses)):
                    if set(results) - set(prev_uses):
                        v.append("wrong_id")
                    if set(prev_uses) - set(results):
                        v.append("missing_result")
                    if len(results) != len(set(results)):
                        v.append("duplicate_result")
        if uses:
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if nxt is None:
                v.append("pending_tool_use")
            elif nxt.get("role") != "user" or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(nxt)):
                v.append("missing_result")
    if msgs[-1].get("role") != "user":
        v.append("tail_not_user")
    return sorted(set(v))


def validate_openai(msgs: List[Msg]) -> List[str]:
    v: List[str] = []
    if not msgs:
        return ["empty"]
    seen_calls = set()
    i, n, head = 0, len(msgs), True
    while i < n:
        m = msgs[i]
        role = m.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            v.append("bad_role")
            i += 1
            continue
        if role in ("system", "developer"):
            if not head:
                v.append("system_not_at_head")
        else:
            head = False
        if role == "tool":
            v.append("orphan_result_or_split_pair")     # a tool message reached outside a group handled below
            i += 1
            continue
        calls = m.get("tool_calls") or []
        if role == "assistant" and calls:
            ids = [c.get("id") for c in calls]
            for cid in ids:
                if cid in seen_calls:
                    v.append("duplicate_tool_use_id")
                seen_calls.add(cid)
            j, got = i + 1, []
            while j < n and msgs[j].get("role") == "tool":
                got.append(msgs[j].get("tool_call_id"))
                if not isinstance(msgs[j].get("content"), str):
                    v.append("bad_tool_content")
                j += 1
            if set(got) - set(ids):
                v.append("wrong_id")
            if set(ids) - set(got):
                v.append("missing_result")
            if len(got) != len(set(got)):
                v.append("duplicate_result")
            i = j
            continue
        i += 1
    if msgs[-1].get("role") not in ("user", "tool"):
        v.append("tail_not_user")
    # head orphan (conversation starting with a tool message) is covered by orphan class above
    return sorted(set(v))


def validate(shape: str, msgs: List[Msg]) -> List[str]:
    return validate_anthropic(msgs) if shape == "anthropic" else validate_openai(msgs)


# ------------------------------------------------------------------ units: a pair is never separated
def units(shape: str, msgs: List[Msg]) -> List[Tuple[int, int]]:
    """Half-open index ranges (start, end) of atomic units: a plain message, or an assistant tool-call message together with its result
    message(s). System/developer head messages are not units (kept by the trimmer)."""
    out, i, n = [], 0, len(msgs)
    while i < n and shape == "openai" and msgs[i].get("role") in ("system", "developer"):
        i += 1
    while i < n:
        m = msgs[i]
        if shape == "anthropic":
            if m.get("role") == "assistant" and any(b.get("type") == "tool_use" for b in _blocks(m)) and i + 1 < n:
                out.append((i, i + 2))
                i += 2
                continue
        else:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                j = i + 1
                while j < n and msgs[j].get("role") == "tool":
                    j += 1
                out.append((i, j))
                i = j
                continue
        out.append((i, i + 1))
        i += 1
    return out


class TrimResult:
    def __init__(self, messages, dropped_units, over_budget, marker_added, tokens):
        self.messages, self.dropped_units, self.over_budget, self.marker_added, self.tokens = messages, dropped_units, over_budget, marker_added, tokens


def _marker(shape):
    return {"role": "user", "content": MARKER_TEXT}


def trim(shape: str, msgs: List[Msg], budget_tokens: int, reserve_tokens: int = 0) -> TrimResult:
    """Drop the oldest units until the conversation fits budget_tokens - reserve_tokens. The last unit (the live turn, possibly a tool
    result and its call) is mandatory. A system head is always kept. If the surviving first message is an assistant, a user marker is added."""
    us = units(shape, msgs)
    head_end = us[0][0] if us else 0
    head = msgs[:head_end]
    limit = budget_tokens - reserve_tokens
    start = 0
    def size(s):
        return total_tokens(head) + sum(total_tokens(msgs[a:b]) for a, b in us[s:])
    while start < len(us) - 1 and size(start) > limit:
        start += 1
    kept = [copy.deepcopy(m) for a, b in us[start:] for m in msgs[a:b]]
    marker = False
    if shape == "anthropic" or kept:
        first = kept[0] if kept else None
        if first is not None and first.get("role") != "user":
            kept = [_marker(shape)] + kept
            marker = True
        elif first is not None and shape == "openai" and first.get("role") == "tool":
            kept = [_marker(shape)] + kept
            marker = True
    out = [copy.deepcopy(m) for m in head] + kept
    t = total_tokens(out)
    return TrimResult(out, start, t > limit, marker, t)


def naive_tail_trim(shape: str, msgs: List[Msg], budget_tokens: int, reserve_tokens: int = 0) -> List[Msg]:
    """CONTROL C1: drop oldest MESSAGES one at a time (message-level, pair-blind), keep the system head for openai."""
    out = list(msgs)
    head = 1 if shape == "openai" and out and out[0].get("role") in ("system", "developer") else 0
    limit = budget_tokens - reserve_tokens
    while len(out) - head > 1 and total_tokens(out) > limit:
        del out[head]
    return copy.deepcopy(out)


# ------------------------------------------------------------------ injection
def inject(shape: str, msgs: List[Msg], block: str) -> List[Msg]:
    """Place the memory block where the provider accepts it. anthropic: in the last user message, AFTER its tool_result blocks and BEFORE
    its other content. openai: if the last message is a user message, prepend to its content; if it is a tool message, append a new user
    message AFTER all tool messages of the group. Empty block: no change."""
    if not block:
        return copy.deepcopy(msgs)
    out = copy.deepcopy(msgs)
    last = out[-1]
    if shape == "anthropic":
        if last.get("role") != "user":
            raise ValueError("last message is not a user message")
        c = last["content"]
        bl = [{"type": "text", "text": c}] if isinstance(c, str) else c
        k = 0
        while k < len(bl) and bl[k].get("type") == "tool_result":
            k += 1
        last["content"] = bl[:k] + [{"type": "text", "text": block}] + bl[k:]
    else:
        if last.get("role") == "user" and isinstance(last.get("content"), str):
            last["content"] = block + "\n\n" + last["content"]
        elif last.get("role") in ("user", "tool"):
            out.append({"role": "user", "content": block})
        else:
            raise ValueError("last message is not a user or tool message")
    return out


def naive_inject(shape: str, msgs: List[Msg], block: str) -> List[Msg]:
    """CONTROL C2: put the block at the FRONT of the last user message (anthropic) / right after the assistant tool_calls message (openai)."""
    if not block:
        return copy.deepcopy(msgs)
    out = copy.deepcopy(msgs)
    if shape == "anthropic":
        last = out[-1]
        c = last["content"]
        bl = [{"type": "text", "text": c}] if isinstance(c, str) else c
        last["content"] = [{"type": "text", "text": block}] + bl
    else:
        k = len(out)
        while k > 0 and out[k - 1].get("role") == "tool":
            k -= 1
        out.insert(k, {"role": "user", "content": block})   # between the assistant tool_calls message and its tool messages
    return out
