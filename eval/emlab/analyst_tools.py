"""Analyst tool backend (read-only, deterministic, exact retrievers; no approximate index, no LLM).

One JSON request on stdin -> one JSON response on stdout (single-shot), or `--serve` for a persistent process that
reads one JSON request per line and writes one response per line (for an MCP stdio wrapper run over ssh).
Scope: a scope file per (level, question) lists the session ids and the dates shown for THIS question; every tool
refuses ids outside it. Scope and task files contain no gold labels and no answer.

Tools: search(query, k<=20, mode=hybrid|lexical|dense), open_round(round_id), open_session(session_id,
from_round, to_round), timeline(terms, limit), search_facts(query, k).
Budget (registered): at most 12 tool calls and 60 s of tool time per question and level, enforced from the call log.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
import time
from typing import Any, Dict, List, Optional

from .fusion import rrf
from .retrievers import tokens

MAX_CALLS = 12
MAX_TOOL_SECONDS = 60.0
SNIPPET = 300
OPEN_CAP = 6000


def build_postings(chunks_path: str, out_path: str) -> None:
    import numpy as np
    from collections import Counter
    post: Dict[str, List] = {}
    dl: List[int] = []
    for i, line in enumerate(open(chunks_path, encoding="utf-8")):
        toks = tokens(json.loads(line)["text"])
        dl.append(len(toks))
        for w, n in Counter(toks).items():
            post.setdefault(w, []).append((i, n))
    packed = {w: (np.asarray([a for a, _ in v], dtype=np.int32), np.asarray([b for _, b in v], dtype=np.int16))
              for w, v in post.items()}
    with open(out_path, "wb") as fh:
        pickle.dump({"n": len(dl), "dl": np.asarray(dl, dtype=np.int32), "post": packed}, fh, protocol=4)


class Backend:
    def __init__(self, art: str, scope: Dict[str, Any], model_dir: Optional[str] = None, facts: Optional[str] = None):
        import numpy as np
        self.np = np
        a = art.rstrip("/") + "/"
        self.scope = scope
        self.sessions = scope["sessions"]  # session id -> date shown for THIS question
        self.chunks = [json.loads(l) for l in open(a + "chunks.jsonl", encoding="utf-8")]
        self.vectors = np.load(a + "chunk_vectors.npy", mmap_mode="r")
        pk = pickle.load(open(a + "postings.pkl", "rb"))
        self.post, self.dl, self.n = pk["post"], pk["dl"], pk["n"]
        self.avgdl = float(self.dl.mean())
        self.round_text: Dict[str, str] = {}
        for line in open(a + "rounds.jsonl", encoding="utf-8"):
            r = json.loads(line)
            if r["session_id"] in self.sessions:
                self.round_text[r["round_id"]] = r["text"]
        self.by_session: Dict[str, List[int]] = {}
        for i, c in enumerate(self.chunks):
            if c["session_id"] in self.sessions:
                self.by_session.setdefault(c["session_id"], []).append(i)
        self.mask = np.zeros(self.n, dtype=bool)
        for ids in self.by_session.values():
            self.mask[ids] = True
        self.model_dir = model_dir
        self._emb = None
        self.facts_path = facts
        self._facts = None

    # -- helpers
    def _round_info(self, rid: str, rank: int) -> Dict[str, Any]:
        sid = rid.rsplit(":", 1)[0]
        return {"round_id": rid, "session_id": sid, "date": self.sessions[sid], "rank": rank,
                "snippet": self.round_text[rid][:SNIPPET]}

    def _rollup(self, idx, scores, limit: int) -> List[str]:
        np = self.np
        order = np.argsort(-scores, kind="stable")
        seen, out = set(), []
        for j in order:
            rid = self.chunks[int(idx[j])]["round_id"]
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
                if len(out) >= limit:
                    break
        return out

    def _lexical(self, terms: List[str], limit: int) -> List[str]:
        np = self.np
        score = np.zeros(self.n, dtype=np.float64)
        for w in set(terms):
            p = self.post.get(w)
            if p is None:
                continue
            idx, tf = p
            m = self.mask[idx]
            idx, tf = idx[m], tf[m].astype(np.float64)
            df = len(p[0])
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            score[idx] += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * self.dl[idx] / self.avgdl))
        pos = np.nonzero(score > 0)[0]
        return self._rollup(pos, score[pos], limit) if len(pos) else []

    def _embed(self, text: str):
        if self._emb is None:
            import torch
            from transformers import AutoModel, AutoTokenizer
            torch.set_num_threads(4)
            name = "intfloat/multilingual-e5-small"
            self._emb = (torch, AutoTokenizer.from_pretrained(name, cache_dir=self.model_dir),
                         AutoModel.from_pretrained(name, cache_dir=self.model_dir).eval())
        torch, tok, model = self._emb
        enc = tok(["query: " + text], padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            h = model(**enc).last_hidden_state
        m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
        return torch.nn.functional.normalize((h * m).sum(1) / m.sum(1), dim=-1).numpy()[0]

    def _dense(self, text: str, limit: int) -> List[str]:
        np = self.np
        idx = np.nonzero(self.mask)[0]
        sims = self.vectors[idx] @ self._embed(text)
        return self._rollup(idx, sims, limit)

    def _check_round(self, rid: str) -> None:
        if rid not in self.round_text:
            raise ValueError("round not in this question's level scope")

    # -- tools
    def search(self, query: str, k: int = 10, mode: str = "hybrid") -> Dict[str, Any]:
        k = max(1, min(int(k), 20))
        if mode == "lexical":
            ids = self._lexical(tokens(query), k)
        elif mode == "dense":
            ids = self._dense(query, k)
        elif mode == "hybrid":
            ids = [d for d, _ in rrf([self._lexical(tokens(query), 200), self._dense(query, 200)], k=60)][:k]
        else:
            raise ValueError("mode must be hybrid, lexical or dense")
        return {"results": [self._round_info(r, i + 1) for i, r in enumerate(ids)]}

    def open_round(self, round_id: str) -> Dict[str, Any]:
        self._check_round(round_id)
        t = self.round_text[round_id]
        sid = round_id.rsplit(":", 1)[0]
        return {"round_id": round_id, "date": self.sessions[sid], "text": t[:OPEN_CAP], "truncated": len(t) > OPEN_CAP}

    def open_session(self, session_id: str, from_round: int = 0, to_round: int = 10 ** 6) -> Dict[str, Any]:
        if session_id not in self.sessions:
            raise ValueError("session not in this question's level scope")
        rounds = sorted((int(r.rsplit(":", 1)[1]), r) for r in self.round_text if r.rsplit(":", 1)[0] == session_id)
        picked = [(i, r) for i, r in rounds if int(from_round) <= i <= int(to_round)]
        out, used = [], 0
        for i, r in picked:
            t = self.round_text[r]
            if used + len(t) > OPEN_CAP and out:
                return {"session_id": session_id, "date": self.sessions[session_id], "rounds": out, "truncated": True}
            out.append({"round_id": r, "text": t[:OPEN_CAP - used]})
            used += len(t)
        return {"session_id": session_id, "date": self.sessions[session_id], "rounds": out, "truncated": False}

    def timeline(self, terms: List[str], limit: int = 20) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        want = set(tokens(" ".join(terms)))
        hits = []
        for rid, t in self.round_text.items():
            if want & set(tokens(t)):
                sid = rid.rsplit(":", 1)[0]
                hits.append((self.sessions[sid], int(rid.rsplit(":", 1)[1]), rid))
        hits.sort()
        return {"results": [dict(self._round_info(r, i + 1)) for i, (_, _, r) in enumerate(hits[:limit])],
                "total_matches": len(hits)}

    def search_facts(self, query: str, k: int = 10) -> Dict[str, Any]:
        if not self.facts_path or not os.path.exists(self.facts_path):
            return {"error": "facts not available yet"}
        from .facts import FactsRetriever, load_facts
        if self._facts is None:
            self._facts = FactsRetriever(load_facts(self.facts_path))
        ids = self._facts.rank(query, [], None, list(self.sessions), 50)[:max(1, min(int(k), 20))]
        by = {f["id"]: f for f in self._facts.facts}
        return {"results": [{"fact_id": i, "session_id": by[i]["session_id"], "date": self.sessions[by[i]["session_id"]],
                             "text": by[i]["text"], "round_id": "%s:%s" % (by[i]["session_id"], by[i]["round_index"])}
                            for i in ids]}

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn = {"search": self.search, "open_round": self.open_round, "open_session": self.open_session,
              "timeline": self.timeline, "search_facts": self.search_facts}.get(tool)
        if fn is None:
            raise ValueError("unknown tool %r" % tool)
        return fn(**args)


def returned_ids(resp: Dict[str, Any]) -> List[str]:
    ids = [r["round_id"] for r in resp.get("results", []) if "round_id" in r]
    if "round_id" in resp:
        ids.append(resp["round_id"])
    ids += [r["round_id"] for r in resp.get("rounds", [])]
    return ids


def budget_state(log_path: str, qid: str, level: str) -> Dict[str, float]:
    calls, secs = 0, 0.0
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            r = json.loads(line)
            if r.get("qid") == qid and r.get("level") == level:
                calls += 1
                secs += float(r.get("seconds", 0))
    return {"calls": calls, "seconds": secs}


def handle(backend: Backend, req: Dict[str, Any], log_path: str, qid: str, level: str) -> Dict[str, Any]:
    st = budget_state(log_path, qid, level)
    if st["calls"] >= MAX_CALLS or st["seconds"] >= MAX_TOOL_SECONDS:
        return {"error": "tool budget exhausted", "budget": {"max_calls": MAX_CALLS, "max_tool_seconds": MAX_TOOL_SECONDS, **st}}
    t0 = time.time()
    try:
        resp = backend.call(req["tool"], req.get("args", {}))
    except Exception as e:  # tool errors are returned, and still logged and counted
        resp = {"error": str(e)[:300]}
    dt = time.time() - t0
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "qid": qid, "level": level, "tool": req.get("tool"),
                             "args": req.get("args", {}), "returned_ids": returned_ids(resp), "seconds": dt,
                             "error": resp.get("error")}) + "\n")
    resp["budget_used"] = {"calls": st["calls"] + 1, "seconds": round(st["seconds"] + dt, 3)}
    return resp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--scope-dir", required=True, help="dir holding <level>/<qid>.json scope files")
    ap.add_argument("--level", required=True, choices=["H1", "H2", "H3"])
    ap.add_argument("--qid", required=True)
    ap.add_argument("--log", required=True, help="per-run call log (JSONL)")
    ap.add_argument("--model-dir")
    ap.add_argument("--facts")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--build-postings", action="store_true")
    a = ap.parse_args(argv)
    if a.build_postings:
        build_postings(os.path.join(a.artifacts, "chunks.jsonl"), os.path.join(a.artifacts, "postings.pkl"))
        return 0
    scope = json.load(open(os.path.join(a.scope_dir, a.level, a.qid + ".json")))
    b = Backend(a.artifacts, scope, a.model_dir, a.facts)
    if a.serve:
        for line in sys.stdin:
            if line.strip():
                print(json.dumps(handle(b, json.loads(line), a.log, a.qid, a.level)), flush=True)
        return 0
    print(json.dumps(handle(b, json.loads(sys.stdin.read()), a.log, a.qid, a.level)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
