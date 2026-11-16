"""Delta review D6: per-role API keys make the Elasticsearch fence structural (THRESHOLDS amendment 13).
Requires security on (python -m wp.es_security enable). Per run (R=3), one tenant, projected by a projector that uses
the PROJECTOR key; then, each attempt by one role's client:
  P_alias     projector writes through its current write alias            -> allowed (positive control)
  P_concrete  projector bulk-writes to the concrete base index, no alias  -> refused (403)
  P_erase     projector writes a document through -x                      -> refused
  E_erase     eraser writes a placeholder through -x                      -> allowed (positive control)
  E_concrete  eraser writes to the base index                             -> refused
  E_walias    eraser writes through the projector's write alias           -> refused
  V_write     verifier writes anywhere (base index)                       -> refused
  V_merge     verifier refresh + force-merge + count                      -> allowed (positive control)
Control: the same attempts with ONE shared key (the admin key, the 963e597 deployment): every refused case succeeds.
Reported, not thresholded: projector DELETE of its base index (its `manage` privilege allows it; residual in
wp/es_keys.py). usage: python -m exp.d6_es_keys [R]"""
import json
import sys

from exp.common import write_result
from exp.util import tenant, msg, session, env
from wp import config, families as F
from wp.es import ES


def _bulk_raw(es, index, doc_id, src, version=None):
    """A raw single-item bulk WITHOUT require_alias (what a dishonest writer sends). Returns the item status."""
    meta = {"_index": index, "_id": doc_id}
    if version is not None:
        meta.update(version=version, version_type="external")
    nd = json.dumps({"index": meta}) + "\n" + json.dumps(src) + "\n"
    s, j = es.req("POST", "/_bulk", ndjson=nd, ok=(200, 401, 403))
    if s != 200:
        return s
    return j["items"][0]["index"]["status"]


def once(shared):
    from wp import log, projector
    conn, ks, _ = env()
    k = {r: (ES() if shared else ES(role=r)) for r in config.ES_ROLES}
    t = tenant("d6")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"X note {i}", "X") for i in range(3)])
    p = projector.Projector(conn, ks, k["projector"], t); p.run(); p.stop()
    walias = [a for a in ES().alias_map(f"{F.P}{t}-events-*").get(F.base_index(t, "events"), set()) if "-w" in a][0]
    base, erase = F.base_index(t, "events"), F.erase_alias(t, "events")
    doc = {"subject_id": "X", "content": "planted d6"}
    out = {
        "P_alias": _bulk_raw(k["projector"], walias, "d6-ok", dict(doc, content="via alias")),
        "P_concrete": _bulk_raw(k["projector"], base, "d6-p1", doc),
        "P_erase": _bulk_raw(k["projector"], erase, "d6-p2", doc),
        "E_erase": _bulk_raw(k["eraser"], erase, "d6-e0", {"erased": True}, version=F.PLACEHOLDER_VERSION),
        "E_concrete": _bulk_raw(k["eraser"], base, "d6-e1", doc),
        "E_walias": _bulk_raw(k["eraser"], walias, "d6-e2", doc),
        "V_write": _bulk_raw(k["verifier"], base, "d6-v1", doc),
    }
    try:
        F.expunge(k["verifier"], t, wait_s=60)
        out["V_merge"] = 200 if k["verifier"].count(f"{F.P}{t}-events-*", {"match_all": {}}) >= 0 else 0
    except Exception as e:  # noqa: BLE001
        out["V_merge"] = getattr(e, "status", None) or type(e).__name__
    s_del, _ = k["projector"].req("DELETE", f"/{F.base_index(t, 'facts')}", ok=(200, 401, 403, 404))
    out["P_delete_index_reported"] = s_del
    return out


ALLOWED = ("P_alias", "E_erase", "V_merge")
REFUSED = ("P_concrete", "P_erase", "E_concrete", "E_walias", "V_write")


def main(r):
    assert config.es_auth_header("projector") and config.es_auth_header() , "security is off: run python -m wp.es_security enable"
    res = {"R": r, "fenced": [once(False) for _ in range(r)], "control_shared_key": [once(True) for _ in range(r)]}
    why = []
    ok2 = lambda v: v in (200, 201)
    for i, x in enumerate(res["fenced"]):
        why += [f"fenced[{i}] {c} not allowed: {x[c]}" for c in ALLOWED if not ok2(x[c])]
        why += [f"fenced[{i}] {c} not refused: {x[c]}" for c in REFUSED if x[c] != 403]
    for i, x in enumerate(res["control_shared_key"]):
        why += [f"control[{i}] {c} refused with the shared key: {x[c]}" for c in REFUSED if not ok2(x[c])]
    res["verdict"] = {"pass": not why, "why": why}
    print("d6_es_keys", res["verdict"], flush=True)
    write_result("d6_es_keys", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
