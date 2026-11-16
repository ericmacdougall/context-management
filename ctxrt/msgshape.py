"""P1: provider-valid conversations. Validators for the two message shapes, a TURN-atomic trimmer, a placement-aware injector, and the broken
variants used as controls. Pure functions, no I/O.

Rules implemented (sources fetched in this session, 2026-11-15/16, confidence high for Anthropic, medium for OpenAI-compatible; NOT live-verified):
 * https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls : "Tool result blocks must immediately follow their corresponding
   tool use blocks"; "tool_result blocks must come FIRST in the content array. Any text must come AFTER all tool results"; "If the assistant turn also
   called a server tool that has no result block yet, the user message must contain only tool_result blocks ... Text after the results ends the turn
   early ... the request then fails with a 400"; "Keep untrusted content inside tool_result blocks rather than system prompts or plain user text blocks";
   an empty tool_result (no content) is valid; tool_result content may be a string or a list of text, image, document, search_result blocks; is_error.
 * https://platform.claude.com/docs/en/build-with-claude/extended-thinking and .../thinking : a tool-use loop is ONE assistant turn; thinking blocks
   (with their signature) are passed back unmodified within it. So thinking blocks are never edited, never separated from their turn, and the trimmer
   never cuts inside a tool loop.
Anthropic shape: {"role","content": str | [blocks]}; blocks: text, thinking{thinking,signature}, redacted_thinking{data}, tool_use, tool_result,
server_tool_use, <name>_tool_result (server tool results, assistant side), image, document.
  A1 first message user; A2 an assistant message with tool_use is IMMEDIATELY followed by a user message that STARTS with exactly one tool_result per tool_use
  id; A3 a tool_result only there, and only before any other block; A4 ids unique; thinking only in assistant messages and with a non-empty signature;
  A5 ends with a user message; A6 an unresolved server_tool_use in the previous assistant message means the user message holds ONLY tool_results;
  A7 text blocks are not empty or whitespace-only.
OpenAI-compatible shape: roles system|developer|user|assistant|tool.
  O1 system only at the head; O2 assistant tool_calls followed by one `tool` message per call id before any other role; O3 no orphan tool message;
  O4 unique call ids; O5 ends with user or tool; O6 an assistant message has content or tool_calls (null content without tool_calls and tool_calls: [] are invalid).
Injection rule (what is implemented): the memory block is untrusted DATA. In the Anthropic shape it goes INSIDE the last tool_result's content when the last
user message carries tool results (valid also when a server tool is unresolved, and the documented place for untrusted content), else as the first text
block of the last user message. In the OpenAI shape it is appended to the last tool message, else inserted as its own user message before the last user
message (never merged into the user's own string).
Prompt caching (design consequence, not measured): the block changes every turn and sits in the LAST message, so the cached prefix ends at the previous
turn; if the host stores history WITHOUT the block, every turn writes a cache entry that the next request does not read (1.25x input price for the
5-minute TTL) and history beyond explicit system/tool breakpoints is effectively uncached; if the host keeps the block in history, caching works but old
blocks accumulate and become re-ingest surfaces. Put a cache breakpoint before the last turn and keep the block out of stored history.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Tuple

Msg = Dict[str, Any]
MARKER_TEXT = "[earlier conversation trimmed to fit the context budget]"
SERVER_RESULT_SUFFIX = "_tool_result"


# ------------------------------------------------------------------ token estimate (deterministic): ASCII chars / 4, non-ASCII chars about 1 token each
def est_tokens(msg: Msg) -> int:
    s = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    ascii_n = sum(1 for ch in s if ord(ch) < 128)
    return max(1, (ascii_n + 3) // 4 + (len(s) - ascii_n))


def total_tokens(msgs: List[Msg]) -> int:
    return sum(est_tokens(m) for m in msgs)


def _blocks(m: Msg) -> List[dict]:
    c = m.get("content")
    return c if isinstance(c, list) else []


def is_server_result(b: dict) -> bool:
    return isinstance(b, dict) and str(b.get("type", "")).endswith(SERVER_RESULT_SUFFIX) and b.get("type") != "tool_result"


def _text_ok(t) -> bool:
    return isinstance(t, str) and t.strip() != ""


# ------------------------------------------------------------------ validators: return a list of violation class names
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
        if c in ("", None, []) or (isinstance(c, str) and not _text_ok(c)):
            v.append("empty_content")
        bl = _blocks(m)
        for b in bl:
            t = b.get("type") if isinstance(b, dict) else None
            if t == "text":
                if not _text_ok(b.get("text")):
                    v.append("empty_text")
            elif t in ("thinking", "redacted_thinking"):
                if role != "assistant":
                    v.append("thinking_in_user")
                if t == "thinking" and not (isinstance(b.get("signature"), str) and b["signature"]):
                    v.append("thinking_without_signature")
                if t == "redacted_thinking" and not b.get("data"):
                    v.append("redacted_without_data")
            elif t in ("tool_use", "server_tool_use"):
                if role != "assistant":
                    v.append("tool_use_in_user")
                if b.get("id") in seen_use:
                    v.append("duplicate_tool_use_id")
                seen_use.add(b.get("id"))
            elif t == "tool_result":
                if role != "user":
                    v.append("tool_result_in_assistant")
                cc = b.get("content")
                if cc is not None and not isinstance(cc, str) and not (isinstance(cc, list) and all(isinstance(x, dict) and x.get("type") in ("text", "image", "document", "search_result") for x in cc)):
                    v.append("bad_tool_result_content")
                if isinstance(cc, list):
                    if any(x.get("type") == "text" and not _text_ok(x.get("text")) for x in cc):
                        v.append("empty_text")
                if "is_error" in b and not isinstance(b["is_error"], bool):
                    v.append("bad_is_error")
            elif t in ("image", "document"):
                if role != "user":
                    v.append("media_in_assistant")
            elif is_server_result(b):
                if role != "assistant":
                    v.append("server_result_in_user")
            else:
                v.append("bad_block")
        uses = [b["id"] for b in bl if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b.get("tool_use_id") for b in bl if isinstance(b, dict) and b.get("type") == "tool_result"]
        prev = msgs[i - 1] if i > 0 else None
        prev_uses = [b["id"] for b in _blocks(prev) if isinstance(b, dict) and b.get("type") == "tool_use"] if prev and prev.get("role") == "assistant" else []
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
                if set(results) - set(prev_uses):
                    v.append("wrong_id")
                if set(prev_uses) - set(results):
                    v.append("missing_result")
                if len(results) != len(set(results)):
                    v.append("duplicate_result")
        if role == "user" and prev and prev.get("role") == "assistant":
            pb = _blocks(prev)
            srv = {b.get("id") for b in pb if isinstance(b, dict) and b.get("type") == "server_tool_use"}
            resolved = {b.get("tool_use_id") for b in pb if is_server_result(b)}
            if srv - resolved and any(not (isinstance(b, dict) and b.get("type") == "tool_result") for b in bl):
                v.append("text_after_unresolved_server_tool")
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
            v.append("orphan_result_or_split_pair")
            i += 1
            continue
        if role == "assistant":
            calls = m.get("tool_calls")
            if calls is not None and not calls:
                v.append("empty_tool_calls")
            if not calls and (m.get("content") is None or (isinstance(m.get("content"), str) and not _text_ok(m["content"]))):
                v.append("assistant_without_content_or_calls")
        if role == "user" and isinstance(m.get("content"), str) and not _text_ok(m["content"]):
            v.append("empty_content")
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
    return sorted(set(v))


def validate(shape: str, msgs: List[Msg]) -> List[str]:
    return validate_anthropic(msgs) if shape == "anthropic" else validate_openai(msgs)


# ------------------------------------------------------------------ units = TURNS (a human message and everything the assistant did before the next human message)
def is_human(shape: str, m: Msg) -> bool:
    if m.get("role") != "user":
        return False
    if shape == "anthropic":
        bl = _blocks(m)
        return not (bl and bl[0].get("type") == "tool_result")
    return True


def units(shape: str, msgs: List[Msg]) -> List[Tuple[int, int]]:
    out, i, n = [], 0, len(msgs)
    while i < n and shape == "openai" and msgs[i].get("role") in ("system", "developer"):
        i += 1
    start = i
    for k in range(i, n):
        if is_human(shape, msgs[k]) and k > start:
            out.append((start, k))
            start = k
    if start < n:
        out.append((start, n))
    return out


def last_human_index(shape: str, msgs: List[Msg]) -> int:
    for k in range(len(msgs) - 1, -1, -1):
        if is_human(shape, msgs[k]):
            return k
    return 0


class TrimResult:
    def __init__(self, messages, dropped_units, over_budget, marker_added, tokens):
        self.messages, self.dropped_units, self.over_budget, self.marker_added, self.tokens = messages, dropped_units, over_budget, marker_added, tokens


def trim(shape: str, msgs: List[Msg], budget_tokens: int, reserve_tokens: int = 0) -> TrimResult:
    """Drop the oldest WHOLE TURNS until the conversation fits budget_tokens - reserve_tokens. The last turn (the live tool loop, including the human
    message that started it and every thinking block in it) is mandatory and is never edited; if it alone exceeds the budget the result is flagged
    over_budget. A system head is kept. The surviving first message is always a human user message, so no marker is needed (marker_added stays False)."""
    us = units(shape, msgs)
    head_end = us[0][0] if us else 0
    head = msgs[:head_end]
    limit = budget_tokens - reserve_tokens
    start = 0

    def size(s):
        return total_tokens(head) + sum(total_tokens(msgs[a:b]) for a, b in us[s:])
    while start < len(us) - 1 and size(start) > limit:
        start += 1
    out = [copy.deepcopy(m) for m in head] + [copy.deepcopy(m) for a, b in us[start:] for m in msgs[a:b]]
    t = total_tokens(out)
    return TrimResult(out, start, t > limit, False, t)


def naive_tail_trim(shape: str, msgs: List[Msg], budget_tokens: int, reserve_tokens: int = 0) -> List[Msg]:
    """CONTROL C1: drop oldest MESSAGES one at a time (message-level, pair-blind and turn-blind), keep the system head for openai."""
    out = list(msgs)
    head = 1 if shape == "openai" and out and out[0].get("role") in ("system", "developer") else 0
    limit = budget_tokens - reserve_tokens
    while len(out) - head > 1 and total_tokens(out) > limit:
        del out[head]
    return copy.deepcopy(out)


def keeps_task(shape: str, orig: List[Msg], out: List[Msg]) -> bool:
    """The human message that started the live tool loop survives unchanged in the output."""
    return orig[last_human_index(shape, orig)] in out


def thinking_intact(orig: List[Msg], out: List[Msg]) -> bool:
    """Every thinking / redacted_thinking block that survives is byte-identical to the original and stays inside its own assistant message."""
    by_msg = {json.dumps(m, sort_keys=True) for m in orig}
    for m in out:
        if any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in _blocks(m)) and json.dumps(m, sort_keys=True) not in by_msg:
            return False
    return True


# ------------------------------------------------------------------ injection
def _tool_result_text(block: dict, text: str) -> None:
    c = block.get("content")
    lst = [] if c is None else ([{"type": "text", "text": c}] if isinstance(c, str) and c.strip() else ([] if isinstance(c, str) else list(c)))
    block["content"] = lst + [{"type": "text", "text": text}]


def inject(shape: str, msgs: List[Msg], block: str) -> List[Msg]:
    """Place the memory block where the provider accepts it and where untrusted content belongs (see the module docstring). Empty block: no change."""
    if not block:
        return copy.deepcopy(msgs)
    out = copy.deepcopy(msgs)
    last = out[-1]
    if shape == "anthropic":
        if last.get("role") != "user":
            raise ValueError("last message is not a user message")
        c = last["content"]
        bl = [{"type": "text", "text": c}] if isinstance(c, str) else c
        trs = [b for b in bl if b.get("type") == "tool_result"]
        if trs:
            _tool_result_text(trs[-1], block)
            last["content"] = bl
        else:
            last["content"] = [{"type": "text", "text": block}] + bl
    else:
        if last.get("role") == "tool":
            last["content"] = (last["content"] + "\n\n" if last["content"] else "") + block
        elif last.get("role") == "user":
            out.insert(len(out) - 1, {"role": "user", "content": block})
        else:
            raise ValueError("last message is not a user or tool message")
    return out


def naive_inject(shape: str, msgs: List[Msg], block: str) -> List[Msg]:
    """CONTROL C2: the block at the FRONT of the last user message (anthropic) / right after the assistant tool_calls message (openai)."""
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
        out.insert(k, {"role": "user", "content": block})
    return out


def text_after_results_inject(shape: str, msgs: List[Msg], block: str) -> List[Msg]:
    """CONTROL C3 (the 1d19cd8 injector): plain user text AFTER the tool results. Valid when every server tool is resolved, 400 when one is not."""
    if not block or shape != "anthropic":
        return copy.deepcopy(msgs)
    out = copy.deepcopy(msgs)
    last = out[-1]
    c = last["content"]
    bl = [{"type": "text", "text": c}] if isinstance(c, str) else c
    k = 0
    while k < len(bl) and bl[k].get("type") == "tool_result":
        k += 1
    last["content"] = bl[:k] + [{"type": "text", "text": block}] + bl[k:]
    return out
