"""Context packing for the answer-level pilot arms (RAG, FACTS, FACTS+RAG). New in phase 3; no LLM here.

Registered rules (see PREREGISTRATION Phase F pilot):
- tokens are estimated as ceil(chars / 4) of the rendered item (a registered estimate, not a tokenizer count);
- a packer walks candidates in rank order and STOPS at the first candidate that does not fit (strict prefix);
- items in the written context are ordered chronologically by the session date of THIS question, ties by
  round index then by rank, identically for every arm; every item shows that date;
- FACTS+RAG gives floor(B/2) to the facts half (facts plus their source rounds) and B - floor(B/2) to the RAG
  half, whose rounds already present from the facts half are skipped (and the walk continues).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Set

ARMS = ("rag", "facts", "facts_rag")


def est_tokens(text: str) -> int:
    return max(1, int(math.ceil(len(text) / 4.0)))


def render_round(date: str, text: str) -> str:
    return "[%s] %s" % (date, text)


def render_fact(date: str, fact: Dict[str, Any]) -> str:
    bits = [fact["text"]]
    for k in ("slot", "value", "modality", "event_time"):
        v = fact.get(k)
        if v not in (None, ""):
            bits.append("%s=%s" % (k, v))
    return "[%s] FACT: %s" % (date, "; ".join(bits))


def _round_item(rid: str, rank: int, dates: Dict[str, str], texts: Dict[str, str], kind: str = "round") -> Dict[str, Any]:
    sid, idx = rid.rsplit(":", 1)
    body = render_round(dates[sid], texts[rid])
    return {"item_id": rid if kind == "round" else "src:" + rid, "kind": kind, "session_id": sid, "round_id": rid,
            "round_index": int(idx), "date": dates[sid], "text": body, "tokens": est_tokens(body), "rank": rank}


def pack_rag(ranked_rounds: Sequence[str], dates: Dict[str, str], texts: Dict[str, str], budget: int,
             skip: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    items, used = [], 0
    for rank, rid in enumerate(ranked_rounds):
        if skip and rid in skip:
            continue
        it = _round_item(rid, rank, dates, texts)
        if used + it["tokens"] > budget:
            break
        items.append(it)
        used += it["tokens"]
    return items


def pack_facts(ranked_fact_ids: Sequence[str], facts: Dict[str, Dict[str, Any]], dates: Dict[str, str],
               texts: Dict[str, str], budget: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    have_rounds: Set[str] = set()
    used = 0
    for rank, fid in enumerate(ranked_fact_ids):
        f = facts[fid]
        rid = "%s:%s" % (f["session_id"], f["round_index"])
        body = render_fact(dates[f["session_id"]], f)
        fi = {"item_id": fid, "kind": "fact", "session_id": f["session_id"], "round_id": rid,
              "round_index": int(f["round_index"]), "date": dates[f["session_id"]], "text": body,
              "tokens": est_tokens(body), "rank": rank}
        new = []
        cost = fi["tokens"]
        if rid not in have_rounds:
            si = _round_item(rid, rank, dates, texts, kind="source_round")
            new.append(si)
            cost += si["tokens"]
        if used + cost > budget:
            break
        items.append(fi)
        items.extend(new)
        have_rounds.add(rid)
        used += cost
    return items


def pack_mix(ranked_fact_ids, ranked_rounds, facts, dates, texts, budget: int) -> List[Dict[str, Any]]:
    half = budget // 2
    f_items = pack_facts(ranked_fact_ids, facts, dates, texts, half)
    present = {i["round_id"] for i in f_items}
    r_items = pack_rag(ranked_rounds, dates, texts, budget - half, skip=present)
    return f_items + r_items


def order_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda i: (i["date"], i["round_index"], i["rank"], i["item_id"]))


def context_metrics(q, items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Is the gold evidence in the packed context? Knowledge-update: stale-only counts as a miss."""
    sess = {i["session_id"] for i in items}
    rounds = {i["round_id"] for i in items}
    gs, gr = q.retrieval_gold_session_ids, q.retrieval_gold_round_ids
    out: Dict[str, Any] = {"tokens_used": sum(i["tokens"] for i in items), "items": len(items),
                           "gold_session_recall": (len(sess & set(gs)) / len(gs)) if gs else None,
                           "gold_round_recall": (len(rounds & set(gr)) / len(gr)) if gr else None,
                           "gold_latest_in_context": None, "stale_only": None}
    latest = q.gold_latest_session_ids
    if latest:
        stale = set(q.gold_session_ids) - set(latest)
        has_latest = bool(sess & set(latest))
        out["gold_latest_in_context"] = has_latest
        out["stale_only"] = (not has_latest) and bool(sess & stale)
    return out


def write_context(root: str, arm: str, level: str, q, budget: int, items: List[Dict[str, Any]]) -> str:
    d = os.path.join(root, arm, level)
    os.makedirs(d, exist_ok=True)
    ordered = order_items(items)
    path = os.path.join(d, "%s.json" % q.qid)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"question_id": q.qid, "arm": arm, "level": level, "budget_tokens": budget,
                   "token_estimate": "ceil(chars/4) of rendered item text",
                   "tokens_used": sum(i["tokens"] for i in ordered), "items": ordered}, fh, indent=1)
    return path
