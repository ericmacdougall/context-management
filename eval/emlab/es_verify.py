"""Read-only verification of the shared index against the local artifacts (ES-pilot threshold T4.1 and T4.5).
Pages every document with search_after on chunk_id through the read-only guard; compares id, round, session, text and the stored
vector (float32) with chunks.jsonl / chunk_vectors.npy; writes counts and digests; also snapshots node and index state."""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Dict

from .es_client import CurlEsClient
from .es_guard import GuardedEs
from .mappings import INDEX_V1

PAGE = 1000


def state(g: GuardedEs, index: str) -> Dict[str, Any]:
    settings = g.get("/%s/_settings" % index)
    cat = g.get("/_cat/indices/%s?format=json&bytes=mb" % index)
    shards = g.get("/_cat/shards?format=json&h=index,shard,prirep,state")
    health = g.get("/_cluster/health")
    stats = g.get("/_nodes/stats/thread_pool")
    rej = {nid: n["thread_pool"]["search"]["rejected"] for nid, n in stats["nodes"].items()}
    row = cat[0] if isinstance(cat, list) else cat
    unassigned = sorted({s["index"] for s in shards if s["state"] != "STARTED"}) if isinstance(shards, list) else None
    return {
        "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
        "hnsw_filter_heuristic": settings[index]["settings"]["index"].get("dense_vector", {}).get("hnsw_filter_heuristic"),
        "index_uuid": settings[index]["settings"]["index"]["uuid"],
        "index_health": row["health"], "docs_count": int(row["docs.count"]), "docs_deleted": int(row["docs.deleted"]),
        "cluster_status": health["status"], "unassigned_shards": health["unassigned_shards"],
        "indices_with_non_started_shards": unassigned, "search_rejected": rej,
    }


def main(artifacts: str, es_url: str, curl_config: str, out_json: str, index: str = INDEX_V1) -> None:
    import numpy as np
    a = artifacts.rstrip("/") + "/"
    g = GuardedEs(CurlEsClient(es_url, curl_config, timeout=300), index)
    before = state(g, index)
    vecs = np.load(a + "chunk_vectors.npy", mmap_mode="r")
    local: Dict[str, Any] = {}
    for i, line in enumerate(open(a + "chunks.jsonl", encoding="utf-8")):
        r = json.loads(line)
        local[r["chunk_id"]] = (r["round_id"], r["session_id"], r["text"], i)
    n_local = len(local)
    seen = set()
    bad_meta, bad_text, bad_vec, missing_vec, extra = 0, 0, 0, 0, 0
    h_es, h_loc = hashlib.sha256(), hashlib.sha256()
    hv_es, hv_loc = hashlib.sha256(), hashlib.sha256()
    after_key = None
    pages = 0
    while True:
        body: Dict[str, Any] = {"size": PAGE, "track_total_hits": False, "sort": [{"chunk_id": "asc"}], "query": {"match_all": {}},
                                "_source": ["chunk_id", "round_id", "session_id", "text", "vector"]}
        if after_key is not None:
            body["search_after"] = [after_key]
        hits = g.search(index, body)["hits"]["hits"]
        if not hits:
            break
        for h in hits:
            s = h["_source"]
            cid = s["chunk_id"]
            after_key = cid
            if cid not in local:
                extra += 1
                continue
            seen.add(cid)
            rid, sid, txt, i = local[cid]
            if s["round_id"] != rid or s["session_id"] != sid:
                bad_meta += 1
            if s["text"] != txt:
                bad_text += 1
            v = s.get("vector")
            if v is None:
                missing_vec += 1
            else:
                va = np.asarray(v, dtype=np.float32)
                if not np.array_equal(va, np.asarray(vecs[i], dtype=np.float32)):
                    bad_vec += 1
            h_es.update(("%s\t%s\t%s\t%s\n" % (cid, s["round_id"], s["session_id"], s["text"])).encode())
            if v is not None:
                hv_es.update(cid.encode() + np.asarray(v, dtype=np.float32).tobytes())
        pages += 1
        if pages % 20 == 0:
            print("verify pages", pages, flush=True)
    for cid in sorted(local):
        rid, sid, txt, i = local[cid]
        h_loc.update(("%s\t%s\t%s\t%s\n" % (cid, rid, sid, txt)).encode())
        hv_loc.update(cid.encode() + np.asarray(vecs[i], dtype=np.float32).tobytes())
    after = state(g, index)
    res = {"index": index, "local_chunks": n_local, "es_docs_paged": len(seen) + extra, "matched_ids": len(seen),
           "missing_in_es": n_local - len(seen), "extra_in_es": extra, "mismatch_round_or_session": bad_meta, "mismatch_text": bad_text,
           "vector_mismatch": bad_vec, "vector_missing_from_source": missing_vec,
           "text_digest_es": h_es.hexdigest(), "text_digest_local": h_loc.hexdigest(),
           "vector_digest_es": hv_es.hexdigest(), "vector_digest_local": hv_loc.hexdigest(),
           "state_before": before, "state_after": after, "request_log": dict(g.log), "search_flags": g.search_flags}
    res["PASS"] = bool(res["missing_in_es"] == 0 and extra == 0 and bad_meta == 0 and bad_text == 0 and bad_vec == 0 and missing_vec == 0
                       and res["text_digest_es"] == res["text_digest_local"] and res["vector_digest_es"] == res["vector_digest_local"]
                       and n_local == 150526)
    json.dump(res, open(out_json, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k not in ("state_before", "state_after")}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
