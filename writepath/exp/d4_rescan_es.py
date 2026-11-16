"""Delta review D4: an event found only by the erasure rescan (its ES document does not carry the subject in
`mentions`) must be verified in Elasticsearch by the store-side verifier, not on the runner's word.

Setup per run: Z writes "lunch with yolanda <mk>" BEFORE "yolanda" is declared for Y (so the write-time matcher cannot
link it); Y writes one message; both are projected; then the identifier is declared. The runner is patched to skip the
placeholders of rescan hits (a faulty or dishonest runner). Fenced (shipped verifier, which checks every tombstoned id
is a placeholder): Y's job is NOT done and the residue names es:tombstoned_not_placeholder. Control (the verifier
without that check, i.e. the 963e597 verifier): the job reports done while Z's ES document still holds <mk>.
Thresholds: R=5, fenced 5/5, control violated 5/5. usage: python -m exp.d4_rescan_es [R]"""
import json
import sys
import uuid

from exp.common import write_result
from exp.util import tenant, msg, session, env, es_count_all
from wp import db, erasure, mentions as M, projector, verifier as V


def once(arm):
    from wp import log
    conn, ks, es = env()
    adm = db.admin()
    t = tenant("d4")
    s = session(conn, t)
    mk = "zqd4" + uuid.uuid4().hex[:6]
    log.append(conn, ks, s, [msg(0, f"lunch with yolanda {mk}", "Z"), msg(1, "Y own words", "Y")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    M.declare(conn, ks, t, "Y", ["yolanda"])
    real_ids, real_ext = erasure.tombstoned_ids, V.Verifier.ext_counts

    def skip_rescan(c, tt, ss, *a, **k):          # faulty runner: no placeholders for rescan hits
        hits = {r[0] for r in adm.execute("select event_id from erasure_mentions where tenant_id=%s and subject_id=%s and source='rescan'", (tt, ss)).fetchall()}
        for page in real_ids(c, tt, ss, *a, **k):
            yield [x for x in page if x[0] not in hits]

    def old_verifier(self, tt, ss, expunge=True):
        out, info = real_ext(self, tt, ss, expunge)
        out.pop("es:tombstoned_not_placeholder", None)
        return out, info
    erasure.tombstoned_ids = skip_rescan
    if arm == "control":
        V.Verifier.ext_counts = old_verifier
    try:
        rec = erasure.erase(conn, ks, es, t, "Y")
    finally:
        erasure.tombstoned_ids, V.Verifier.ext_counts = real_ids, real_ext
    found = rec["steps"].get("rescan_found")
    left = es_count_all(es, t, {"match": {"content": mk}})
    w = {"done": rec["done"], "residue": rec.get("residue"), "rescan_found": found, "es_docs_with_marker": left}
    if found != 1:
        raise RuntimeError(f"setup: rescan found {found}, expected 1")
    return (not rec["done"]) and "es:tombstoned_not_placeholder" in (rec.get("residue") or ""), w


def main(R):
    res = {"R": R}
    for arm in ("fenced", "control"):
        runs = []
        for _ in range(R):
            try:
                h, w = once(arm)
                runs.append({"holds": h, "witness": w})
            except Exception as e:  # noqa: BLE001
                runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        res[arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                    "errors": sum(r["holds"] is None for r in runs), "runs": runs}
    res["pass"] = res["fenced"]["holds"] == R and res["control"]["violated"] == R
    print("D4", "PASS" if res["pass"] else "FAIL", json.dumps({a: (res[a]["holds"], res[a]["violated"], res[a]["errors"]) for a in ("fenced", "control")}),
          json.dumps(res["fenced"]["runs"][0], default=str)[:500], json.dumps(res["control"]["runs"][0], default=str)[:500])
    write_result("d4_rescan_es", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
