"""Client-side rank fusion and the deterministic stand-in embedding."""
import hashlib
import math
import re

DIM = 64
_TOK = re.compile(r"[a-z0-9]+")


def tokens(text):
    return _TOK.findall(text.lower())


def embed(text, dim=DIM):
    """Feature-hashing bag-of-words, L2 normalised, rounded for cross-platform
    determinism. A stand-in for a real embedding model."""
    v = [0.0] * dim
    for tok in tokens(text):
        h = hashlib.sha256(tok.encode()).digest()
        v[int.from_bytes(h[:4], "big") % dim] += 1.0 if h[4] & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return [0.0] * dim
    return [round(x / n, 6) for x in v]


def cosine(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def rrf(ranked_lists, k=60, topn=10):
    """Reciprocal rank fusion. ranked_lists: lists of ids, best first. Ties are
    broken by id so the output is deterministic."""
    score = {}
    for lst in ranked_lists:
        seen = set()
        for rank, did in enumerate(lst, start=1):
            if did in seen:
                continue
            seen.add(did)
            score[did] = score.get(did, 0.0) + 1.0 / (k + rank)
    return sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:topn]


def lexical_score(query_tokens, doc_tokens):
    """Tail-scan lexical score: distinct query terms present (idf-free)."""
    ds = set(doc_tokens)
    return sum(1 for t in set(query_tokens) if t in ds)
