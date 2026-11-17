"""Chunk the unique round corpus and embed chunks and questions with multilingual-e5-small (CPU).

Needs numpy, torch, transformers (see requirements.txt); NOT imported by the stdlib scaffold or its tests.
Run: python3 embed.py --dataset D --model-dir M --out O [--threads 12] [--batch 32] [--limit-chunks N]
Resumable: finished shards are kept in O/shards and skipped on restart.
Outputs in O: rounds.jsonl, chunks.jsonl (chunk_id, round_id, session_id, text), chunk_vectors.npy
(float32, N x 384, row i = chunks.jsonl line i, L2-normalised), queries.json (qid list), query_vectors.npy,
embed_stats.json (rate, wall time, sizes).
"""
import argparse
import json
import os
import time

from emlab.corpus import CHUNK_TOKENS, load_all, write_rounds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--model-dir", required=True, help="HF cache/dir for intfloat/multilingual-e5-small")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--shard", type=int, default=8192)
    ap.add_argument("--limit-chunks", type=int, default=0, help="rate test: embed only the first N chunks")
    a = ap.parse_args()
    os.environ["OMP_NUM_THREADS"] = str(a.threads)
    os.environ["MKL_NUM_THREADS"] = str(a.threads)
    os.environ.setdefault("HF_HOME", a.model_dir)
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(a.threads)
    os.makedirs(os.path.join(a.out, "shards"), exist_ok=True)
    t0 = time.time()
    qs = load_all(a.dataset)
    n_rounds = write_rounds(qs, os.path.join(a.out, "rounds.jsonl"))
    name = "intfloat/multilingual-e5-small"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=a.model_dir)
    model = AutoModel.from_pretrained(name, cache_dir=a.model_dir).eval()
    rev = getattr(model.config, "_commit_hash", None)

    chunks_path = os.path.join(a.out, "chunks.jsonl")
    if not os.path.exists(chunks_path):
        with open(os.path.join(a.out, "rounds.jsonl"), encoding="utf-8") as fh, \
                open(chunks_path + ".tmp", "w", encoding="utf-8") as out:
            for line in fh:
                r = json.loads(line)
                enc = tok(r["text"], add_special_tokens=False, return_offsets_mapping=True, truncation=False,
                          verbose=False)
                offs = enc["offset_mapping"]
                spans = [(0, len(r["text"]))] if len(offs) <= CHUNK_TOKENS else []
                if not spans:
                    for i in range(0, len(offs), CHUNK_TOKENS):
                        w = offs[i:i + CHUNK_TOKENS]
                        spans.append((w[0][0] if i else 0, w[-1][1] if i + CHUNK_TOKENS < len(offs) else len(r["text"])))
                for c, (s, e) in enumerate(spans):
                    out.write(json.dumps({"chunk_id": "%s#%d" % (r["round_id"], c), "round_id": r["round_id"],
                                          "session_id": r["session_id"],
                                          "text": r["text"][s:e]}) + "\n")
        os.replace(chunks_path + ".tmp", chunks_path)
    chunks = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
    if a.limit_chunks:
        chunks = chunks[:a.limit_chunks]
    t_chunk = time.time() - t0

    def embed(texts):
        enc = tok(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            h = model(**enc).last_hidden_state
        m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
        v = (h * m).sum(1) / m.sum(1)
        return torch.nn.functional.normalize(v, dim=-1).numpy()

    # queries first (small)
    qpath = os.path.join(a.out, "query_vectors.npy")
    if not os.path.exists(qpath):
        qv = np.concatenate([embed(["query: " + q.question for q in qs[i:i + a.batch]])
                             for i in range(0, len(qs), a.batch)])
        json.dump([q.qid for q in qs], open(os.path.join(a.out, "queries.json"), "w"))
        np.save(qpath, qv.astype("float32"))

    lens = [len(c["text"]) for c in chunks]
    order = sorted(range(len(chunks)), key=lambda i: lens[i])
    t1 = time.time()
    done_chunks = 0
    for si, s in enumerate(range(0, len(order), a.shard)):
        sp = os.path.join(a.out, "shards", "s%05d.npy" % si)
        idx = order[s:s + a.shard]
        if os.path.exists(sp):
            continue
        parts = [embed(["passage: " + chunks[i]["text"] for i in idx[j:j + a.batch]])
                 for j in range(0, len(idx), a.batch)]
        np.save(sp + ".tmp.npy", np.concatenate(parts).astype("float32"))
        os.replace(sp + ".tmp.npy", sp)
        done_chunks += len(idx)
        el = time.time() - t1
        print("shard %d: %d/%d chunks, %.1f chunks/s" % (si, s + len(idx), len(order), done_chunks / el), flush=True)
    vecs = np.zeros((len(chunks), 384), dtype="float32")
    for si, s in enumerate(range(0, len(order), a.shard)):
        vecs[order[s:s + a.shard]] = np.load(os.path.join(a.out, "shards", "s%05d.npy" % si))
    np.save(os.path.join(a.out, "chunk_vectors.npy"), vecs)
    wall = time.time() - t0
    stats = {"rounds": n_rounds, "chunks": len(chunks), "chunk_seconds": t_chunk,
             "embed_seconds": time.time() - t1, "embedded_this_run": done_chunks,
             "chunks_per_s_this_run": done_chunks / max(1e-9, time.time() - t1), "wall_seconds": wall,
             "threads": a.threads, "batch": a.batch, "model": name, "model_revision": rev,
             "torch": torch.__version__, "chunk_tokens": CHUNK_TOKENS}
    json.dump(stats, open(os.path.join(a.out, "embed_stats.json"), "w"), indent=1)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
