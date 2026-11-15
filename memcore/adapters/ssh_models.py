"""local-e5 and local-bge: thin ssh wrappers around the retrieval owner's models in ~/emlab-16096 on t2-dev (read only).
The remote side is memcore/remote/models_server.py (a one-shot JSON-in/JSON-out script copied to ~/memcore-g5; it only READS the HF cache).
Same port contract as every Embedder: the port applies the E5 prefixes ("query: " / "passage: ")."""
from __future__ import annotations

import json
import subprocess
from typing import List, Optional, Sequence

from ..ports import BackendError, Embedder, Reranker, RerankHit

DEFAULT_SSH = ["ssh", "-F", "~/.ssh/config_fleetops", "t2-dev"]
REMOTE = "cd ~/memcore-g5 && OMP_NUM_THREADS=4 ~/emlab-16096/venv/bin/python models_server.py"


def _run(op: dict, ssh: Sequence[str], timeout_s: float):
    import os
    cmd = [os.path.expanduser(a) for a in ssh] + [REMOTE]
    p = subprocess.run(cmd, input=json.dumps(op), capture_output=True, text=True, timeout=timeout_s)
    if p.returncode != 0:
        raise BackendError("ssh model call failed: %s" % p.stderr[-300:])
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise BackendError("bad reply from remote model script: %r" % p.stdout[-200:])


class LocalE5Embedder(Embedder):
    model, dimensions = "intfloat/multilingual-e5-small", 384
    query_prefix, passage_prefix = "query: ", "passage: "

    def __init__(self, ssh: Sequence[str] = DEFAULT_SSH, timeout_s: float = 300.0):
        self.ssh, self.timeout_s = ssh, timeout_s

    def _embed(self, texts, kind):
        return _run({"op": "embed", "texts": texts}, self.ssh, self.timeout_s)["vectors"]


class LocalBgeReranker(Reranker):
    model = "BAAI/bge-reranker-v2-m3"

    def __init__(self, ssh: Sequence[str] = DEFAULT_SSH, timeout_s: float = 600.0):
        self.ssh, self.timeout_s = ssh, timeout_s

    def rerank(self, query, documents, top_n=None):
        sc = _run({"op": "rerank", "query": query, "docs": list(documents)}, self.ssh, self.timeout_s)["scores"]
        hits = sorted((RerankHit(i, s) for i, s in enumerate(sc)), key=lambda h: (-h.score, h.index))
        return hits[:top_n] if top_n else hits
