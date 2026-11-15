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

## 3b. Pooled-history curve (pilot only; needs numpy, exact retriever)
```
cd eval && ../venv/bin/python -m emlab.runner --phase R --retriever exact --action curve --split pilot --dataset-path ../data/longmemeval_s_cleaned.json --artifacts ../out/emb --out ../out/curve_pilot.json
```

## 4. Elasticsearch node (handed over; ES 9.5.6, https://127.0.0.1:19200 from t2-dev)
The node is shared: one index only (`emlab-rounds-v1`, already created, 0 replicas), modest bulk, never touch other indices, the licence, ML deployments or container settings. The curl config file holds the credential and Content-Type; it is passed by path only and never printed.
```
cd eval && ../venv/bin/python -m emlab.indexer --es-url https://127.0.0.1:19200 --curl-config /home/eric/emlab-16096/curl_key.conf --artifacts ../out/emb
cd eval && ../venv/bin/python -m emlab.escompare --action knn --es-url https://127.0.0.1:19200 --curl-config /home/eric/emlab-16096/curl_key.conf --artifacts ../out/emb --dataset-path ../data/longmemeval_s_cleaned.json --out ../out/es_knn_pilot.json
(same with --action fusion and --action e5)
```

## 4b. Legacy plain-HTTP interface (not used on this node)
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
