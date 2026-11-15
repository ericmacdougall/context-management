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

from .anon import opaque_rid, opaque_sid
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
    """Global read-only index plus a per-call scope. Every id a model sees or sends is OPAQUE (emlab.anon)."""

    def __init__(self, art: str, model_dir: Optional[str] = None, facts: Optional[str] = None):
        import numpy as np
        self.np = np
        a = art.rstrip("/") + "/"
        self.chunks = [json.loads(l) for l in open(a + "chunks.jsonl", encoding="utf-8")]
        self.vectors = np.load(a + "chunk_vectors.npy", mmap_mode="r")
        pk = pickle.load(open(a + "postings.pkl", "rb"))
        self.post, self.dl, self.n = pk["post"], pk["dl"], pk["n"]
        self.avgdl = float(self.dl.mean())
        self.round_text: Dict[str, str] = {}
        for line in open(a + "rounds.jsonl", encoding="utf-8"):
            r = json.loads(line)
            self.round_text[r["round_id"]] = r["text"]
        self.by_session: Dict[str, List[int]] = {}
        for i, c in enumerate(self.chunks):
            self.by_session.setdefault(c["session_id"], []).append(i)
        self.model_dir, self._emb, self.facts_path, self._facts = model_dir, None, facts, None
        self._scopes: Dict[tuple, Any] = {}
        self.set_scope = self._set_scope

    def _set_scope(self, scope: Dict[str, Any]) -> None:
        key = (scope["level"], scope["question_id"])
        if key not in self._scopes:
            if len(self._scopes) > 6:
                self._scopes.pop(next(iter(self._scopes)))
            np = self.np
            mask = np.zeros(self.n, dtype=bool)
            for sid in scope["sessions"]:
                ids = self.by_session.get(sid)
                if ids:
                    mask[ids] = True
            rev = {opaque_sid(s): s for s in scope["sessions"]}
            self._scopes[key] = (scope["sessions"], mask, rev)
        self.sessions, self.mask, self.rev = self._scopes[key]

    # -- id helpers
    def _real_round(self, orid: str) -> str:
        sid, idx = orid.rsplit(":", 1)
        if sid not in self.rev:
            raise ValueError("id not in this question's level scope")
        rid = "%s:%s" % (self.rev[sid], idx)
        if rid not in self.round_text:
            raise ValueError("id not in this question's level scope")
        return rid

    def _round_info(self, rid: str, rank: int) -> Dict[str, Any]:
        sid = rid.rsplit(":", 1)[0]
        return {"round_id": opaque_rid(rid), "session_id": opaque_sid(sid), "date": self.sessions[sid], "rank": rank,
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

    # -- tools (argument names match the runner: search(query,k[,mode]), open_round(round_id), open_session(session_id),
    #    timeline(term)); every id in and out is opaque
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
        rid = self._real_round(round_id)
        t = self.round_text[rid]
        return {"round_id": round_id, "date": self.sessions[rid.rsplit(":", 1)[0]], "text": t[:OPEN_CAP],
                "truncated": len(t) > OPEN_CAP}

    def open_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self.rev:
            raise ValueError("id not in this question's level scope")
        sid = self.rev[session_id]
        rounds = sorted((int(c["round_id"].rsplit(":", 1)[1]), c["round_id"]) for c in
                        (self.chunks[i] for i in self.by_session[sid]) if c["chunk_id"].endswith("#0"))
        return {"session_id": session_id, "date": self.sessions[sid],
                "rounds": [{"round_id": opaque_rid(r), "snippet": self.round_text[r][:SNIPPET]} for _, r in rounds[:60]]}

    def timeline(self, term: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        want = set(tokens(term)) if term else set()
        by: Dict[str, List[str]] = {}
        for sid in self.sessions:
            if sid in self.by_session:
                by[sid] = [c for c in (self.chunks[i] for i in self.by_session[sid])]
        out = []
        for sid, cs in by.items():
            texts = [self.round_text[c["round_id"]] for c in cs if c["chunk_id"].endswith("#0")]
            if want and not any(want & set(tokens(t)) for t in texts):
                continue
            first = (texts[0][:80] if texts else "")
            out.append((self.sessions[sid], sid, first))
        out.sort()
        return {"sessions": [{"session_id": opaque_sid(s), "date": d, "first_words": f} for d, s, f in out[:limit]],
                "total_matches": len(out)}

    def search_facts(self, query: str, k: int = 10) -> Dict[str, Any]:
        return {"error": "facts not available"}

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


def load_scope(scope_dir: str, level: str, qid: str) -> Dict[str, Any]:
    return json.load(open(os.path.join(scope_dir, level, qid + ".json")))


def serve_socket(art: str, scope_dir: str, sock_path: str, log_path: str, model_dir: Optional[str]) -> None:
    """Daemon: holds the index in memory; one JSON object per connection line: {"qid","level","request"}."""
    import socket
    b = Backend(art, model_dir)
    if os.path.exists(sock_path):
        os.remove(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            msg = json.loads(data)
            try:
                b.set_scope(load_scope(scope_dir, msg["level"], msg["qid"]))
                resp = handle(b, msg["request"], log_path, msg["qid"], msg["level"])
            except Exception as e:
                resp = {"error": str(e)[:300]}
            conn.sendall((json.dumps(resp) + "\n").encode())
        finally:
            conn.close()


def client(sock_path: str, qid: str, level: str, req: Dict[str, Any]) -> Dict[str, Any]:
    import socket
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(sock_path)
    except OSError:
        return {"error": "analyst daemon not running"}
    c.sendall((json.dumps({"qid": qid, "level": level, "request": req}) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = c.recv(1 << 20)
        if not chunk:
            break
        data += chunk
    return json.loads(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Analyst tool program. Client mode (default): --question Q --level L, one JSON "
                                 "request on stdin, one JSON response on stdout (talks to the local daemon over a unix socket). "
                                 "Daemon mode: --daemon.")
    ap.add_argument("--artifacts", default=os.path.expanduser("~/emlab-16096/out/emb"))
    ap.add_argument("--scope-dir", default=os.path.expanduser("~/emlab-16096/out/analyst_scope"))
    ap.add_argument("--socket", default=os.path.expanduser("~/emlab-16096/run/analyst.sock"))
    ap.add_argument("--log", default=os.path.expanduser("~/emlab-16096/out/analyst_calls.jsonl"))
    ap.add_argument("--question")
    ap.add_argument("--level", choices=["H1", "H2", "H3"])
    ap.add_argument("--model-dir", default=os.path.expanduser("~/emlab-16096/models"))
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--build-postings", action="store_true")
    a = ap.parse_args(argv)
    if a.build_postings:
        build_postings(os.path.join(a.artifacts, "chunks.jsonl"), os.path.join(a.artifacts, "postings.pkl"))
        return 0
    if a.daemon:
        os.makedirs(os.path.dirname(a.socket), exist_ok=True)
        serve_socket(a.artifacts, a.scope_dir, a.socket, a.log, a.model_dir)
        return 0
    print(json.dumps(client(a.socket, a.question, a.level, json.loads(sys.stdin.read()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
