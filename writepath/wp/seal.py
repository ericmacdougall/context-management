"""Row 3: a checkpoint seal over memory, for the context-runtime stream to consume (its seal is a stub today).

Interface (stable; plain dicts, JSON-serialisable):
  seal(conn, ks, es, tenant, note="") -> Seal dict
      {"v": 1, "tenant", "sealed_at", "memory_watermark", "manifest_hash", "manifest": {...}, "projection_generation",
       "versions": {"secret_scan", "matcher"}, "key_store", "note", "mac"}
  verify(conn, ks, es, sealed) -> {"ok": bool, "problems": [...]}
      ok iff the MAC is valid under the tenant chain key AND every session head named in the manifest is still in the
      log with the same link AND the keyed chain verifies up to it AND the projection generation still exists.
  memory_watermark: per projection family the (commit_xid, ingest_seq) cursor the projector has durably passed, plus
      the database snapshot xmin at seal time: everything below it is either in the projection or filtered (erased,
      tombstoned). A context built from memory under this seal can cite exactly this state.
  manifest_hash: sha256 over the sorted list of (session_id, head seq, head link) of every chain of the tenant.
  projection_generation: the read alias of each family and the concrete index (and its creation uuid) it points to.
The MAC is an HMAC under the tenant chain key (row 6), so a seal cannot be forged or edited by anyone without the key
store; a seal does NOT prevent later appends (it proves a prefix), and an erasure after sealing is expected to make
verify report the erased sessions' heads as still present (the chain covers digests, not content)."""
import hashlib
import hmac
import json
import time

from . import families as F, log, mentions as M, secrets as SEC


def _manifest(conn, tenant):
    rows = conn.execute("select session_id, next_seq - 1, last_hash from chains where tenant_id=%s and next_seq > 1 order by session_id", (tenant,)).fetchall()
    heads = [[s, int(q), h] for s, q, h in rows]
    return heads, hashlib.sha256(json.dumps(heads, separators=(",", ":")).encode()).hexdigest()


def _generation(es, tenant):
    out = {}
    for f in F.FAMILIES:
        s, j = es.req("GET", f"/_alias/{F.read_alias(tenant, f)}", ok=(200, 404))
        idx = sorted(j.keys()) if s == 200 else []
        uu = {}
        for i in idx:
            s2, j2 = es.req("GET", f"/{i}/_settings/index.uuid", ok=(200, 404))
            uu[i] = j2.get(i, {}).get("settings", {}).get("index", {}).get("uuid") if s2 == 200 else None
        out[f] = {"alias": F.read_alias(tenant, f), "indices": uu}
    return out


def _mac(ks, tenant, body):
    return hmac.new(ks.chain_key(tenant), json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def seal(conn, ks, es, tenant, note=""):
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read")
        xmin = int(conn.execute("select pg_snapshot_xmin(pg_current_snapshot())::text").fetchone()[0])
        cur = {f: [int(cx), int(cs)] for f, cx, cs in conn.execute("select family, cx, cs from cursors where tenant_id=%s order by family", (tenant,)).fetchall()}
        heads, mh = _manifest(conn, tenant)
    body = {"v": 1, "tenant": tenant, "sealed_at": time.time(), "memory_watermark": {"cursors": cur, "snapshot_xmin": xmin},
            "manifest_hash": mh, "manifest": {"heads": heads}, "projection_generation": _generation(es, tenant),
            "versions": {"secret_scan": SEC.VERSION, "matcher": M.MATCHER}, "key_store": ks.describe(), "note": note}
    body["mac"] = _mac(ks, tenant, body)
    return body


def verify(conn, ks, es, sealed):
    problems = []
    body = {k: v for k, v in sealed.items() if k != "mac"}
    if not hmac.compare_digest(_mac(ks, sealed["tenant"], body), sealed.get("mac", "")):
        problems.append("mac_invalid")
    t = sealed["tenant"]
    heads = sealed["manifest"]["heads"]
    if hashlib.sha256(json.dumps(heads, separators=(",", ":")).encode()).hexdigest() != sealed["manifest_hash"]:
        problems.append("manifest_hash_mismatch")
    for sid, seq, link in heads:
        r = conn.execute("select hash from events where tenant_id=%s and session_id=%s and seq=%s", (t, sid, seq)).fetchone()
        if r is None:
            problems.append(f"head_missing:{sid}@{seq}")
        elif r[0] != link:
            problems.append(f"head_rewritten:{sid}@{seq}")
        elif not log.verify_chain(conn, t, sid, ks, upto=seq)[0]:
            problems.append(f"chain_invalid:{sid}")
    gen = _generation(es, t)
    for f, g in sealed["projection_generation"].items():
        if any(u != gen.get(f, {}).get("indices", {}).get(i) for i, u in g["indices"].items()):
            problems.append(f"generation_changed:{f}")
    return {"ok": not problems, "problems": problems}
