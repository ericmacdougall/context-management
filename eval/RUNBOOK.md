# Runbook: Phase R on t2-dev

All work is under `~/emlab-16096` on t2-dev (budget: 6 GB disk, at most 12 CPU threads under `nice`, stop and report if free disk would fall below 25 GB). The Elasticsearch container `emlab-es-16096` belongs to another owner: do not touch or index into it until main says so. No credential is ever typed on a command line or placed in an environment variable.

Layout: `data/` dataset, `models/` HF cache, `venv/`, `eval/` this directory, `out/` artifacts, `logs/`.

## 1. Data and environment (done once)
```
curl -L -o data/longmemeval_s_cleaned.json https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
sha256sum data/longmemeval_s_cleaned.json   # d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442
python3 -m venv venv && venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple torch==2.9.1+cpu transformers==4.57.1 numpy==2.2.6
```
Re-measure the dataset facts: `cd eval && ../venv/bin/python -c "import json; from emlab.corpus import verify_dataset; print(json.dumps(verify_dataset('../data/longmemeval_s_cleaned.json'), indent=1))"`.

## 2. Chunk and embed (detached; resumable from out/emb/shards)
```
cd eval && nohup nice -n 10 ../venv/bin/python embed.py --dataset ../data/longmemeval_s_cleaned.json --model-dir ../models --out ../out/emb --threads 12 --batch 32 > ../logs/embed.log 2>&1 &
tail -f ../logs/embed.log
```
Outputs: rounds.jsonl, chunks.jsonl, chunk_vectors.npy (float32, N x 384), queries.json, query_vectors.npy, embed_stats.json.

## 3. Pilot-only pre-computation and tuning (before the pre-registration is frozen)
```
cd eval && ../venv/bin/python -m emlab.runner --phase R --retriever exact --action lowoverlap --split pilot --dataset-path ../data/longmemeval_s_cleaned.json --artifacts ../out/emb --out ../out/lowoverlap_pilot.json
cd eval && ../venv/bin/python -m emlab.runner --phase R --retriever exact --action grid --split pilot --dataset-path ../data/longmemeval_s_cleaned.json --artifacts ../out/emb --out ../out/grid_pilot.json
```
Do not pass `--split test` or `--split all` until main has recorded the frozen hash of PREREGISTRATION.md on the ticket.

## 4. After handover of the Elasticsearch node (not before)
```
cd eval && ../venv/bin/python -m emlab.indexer --es-url http://127.0.0.1:<ES_PORT> --credential-file <CRED_FILE> --artifacts ../out/emb --index emlab-chunks
cd eval && ../venv/bin/python -m emlab.runner --phase R --retriever es --es-url http://127.0.0.1:<ES_PORT> --credential-file <CRED_FILE> --index emlab-chunks --split pilot --dataset-path ../data/longmemeval_s_cleaned.json --artifacts ../out/emb --out ../out/phaseR_pilot.json
```
Add `--reranker-dir ../models` to enable the rerank arm (needs BAAI/bge-reranker-v2-m3 in the cache). `<CRED_FILE>` is mode 0600 and outside the repo. The client refuses non-loopback hosts.

## 5. Tests and dry run (anywhere, stdlib only)
```
cd eval && python3 -m unittest
cd eval && python3 -m emlab.runner --dry-run
```
