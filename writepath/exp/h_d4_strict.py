"""Amendment H3 (D4): the verifier checks content ABSENCE per tombstoned id, not a count.

Per run: a fresh tenant; Y writes two messages, one carrying a marker <mk>; both are projected; Y is erased
(physical='wait') with one fault injected between the placeholders and the verifier:
  content   after the placeholders, the operator client re-indexes Y's marked event at version 2^62 (external_gte)
            with {"erased": true, "content": "... <mk>"}: a placeholder still carrying content
  missing   after the placeholders, the operator client deletes the placeholder of Y's marked event
  version   the runner writes its placeholders at external version 5 instead of 2^62 (the event documents are
            created at internal version 1, so 5 overwrites them): a placeholder a later write could overwrite
Arms: fenced = the shipped verifier (wp/es_verify.py); control = the fd5d2a3 check (count of documents among the ids
that lack erased:true), all other verifier counts unchanged.
holds (fenced): the job is NOT done and the residue names the expected key (es:placeholder_content / _missing /
_version). violated (control): the job reports done while the witness shows the injected state (a document with <mk>,
no document, or _version 5). The witness is read by the operator client with GET by id on the base index.
Stale replica: NOT run here (single node, number_of_replicas 0); shown offline only (tests/test_es_verify.py 5 and 6).
usage: python -m exp.h_d4_strict [R]"""
import json
import sys
import uuid

from exp.common import write_result
from exp.util import tenant, msg, session, env, drop_tenant
from wp import db, erasure, es_verify as EV, families as F, keys as K, log, projector, verifier as V

CASES = {"content": "es:placeholder_content", "missing": "es:placeholder_missing", "version": "es:placeholder_version"}


def _old_ext(real):
    """The fd5d2a3 placeholder count in place of the strict keys (control arm)."""
    def ext(self, t, s, expunge=True, wait_s=180.0):
        out, info = real(self, t, s, expunge, wait_s)
        for k in EV.KEYS:
            out.pop(k, None)
        bad = 0
        for page in self._tombstoned(t, s):
            ids = [e for e, _ in page]
            for f, pg in (("events", ids), ("facts", [K.derive_id(t, e) for e in ids])):
                q = {"bool": {"filter": [{"ids": {"values": pg}}], "must_not": [{"term": {"erased": True}}]}}
                bad += self.es.count(f"{F.P}{t}-{f}-*", q)
        out["es:tombstoned_not_placeholder"] = bad
        return out, info
    return ext


def _witness(es, t, eid):
    base = F.base_index(t, "events")
    es.req("POST", f"/{base}/_refresh", ok=(200, 404))
    s, j = es.req("GET", f"/{base}/_doc/{eid}", ok=(200, 404))
    if s == 404 or not j.get("found"):
        return {"present": False}
    src = j.get("_source") or {}
    return {"present": True, "version": j.get("_version"), "fields": sorted(src), "has_content": "content" in src}


def once(case, arm):
    conn, ks, es = env()
    t = tenant("h3")
    s = session(conn, t)
    mk = "zqh3" + uuid.uuid4().hex[:6]
    log.append(conn, ks, s, [msg(0, f"note {mk}", "Y"), msg(1, "Y other words", "Y")])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    with db.admin() as adm:                                 # the first appended event is the marked one
        eid = adm.execute("select event_id from events where tenant_id=%s order by seq limit 1", (t,)).fetchone()[0]
    base = F.base_index(t, "events")
    real_ext, real_ver = V.Verifier.ext_counts, F.PLACEHOLDER_VERSION

    def hook(step):
        if case == "version" and step == "after_cascade":
            F.PLACEHOLDER_VERSION = 5                      # faulty runner
        if step == "after_placeholders":
            F.PLACEHOLDER_VERSION = real_ver               # the verifier reads the true constant
            if case == "content":
                es.req("PUT", f"/{base}/_doc/{eid}", {"erased": True, "content": f"note {mk}"}, ok=(200, 201),
                       version=real_ver, version_type="external_gte", refresh="true")
            elif case == "missing":
                es.req("DELETE", f"/{base}/_doc/{eid}", ok=(200,), refresh="true")
    if arm == "control":
        V.Verifier.ext_counts = _old_ext(real_ext)
    try:
        rec = erasure.erase(conn, ks, es, t, "Y", physical="wait", hook=hook)
    finally:
        V.Verifier.ext_counts, F.PLACEHOLDER_VERSION = real_ext, real_ver
    w = {"done": rec["done"], "receipt_state": rec.get("receipt_state"), "residue": rec.get("residue"),
         "error_type": rec.get("error_type"), "doc": _witness(es, t, eid)}
    injected = {"content": w["doc"].get("has_content") is True, "missing": w["doc"]["present"] is False,
                "version": w["doc"].get("version") == 5}[case]
    drop_tenant(es, t)
    if not injected:
        raise RuntimeError(f"setup: fault {case} not visible in the witness {w['doc']}")
    if arm == "fenced":
        return (not rec["done"]) and CASES[case] in (rec.get("residue") or ""), w
    return not rec["done"], w                               # control: holds only if the old check also refused


def main(R):
    res = {"R": R, "cases": {}}
    ok = True
    for case in CASES:
        res["cases"][case] = {}
        for arm in ("fenced", "control"):
            runs = []
            for _ in range(R):
                try:
                    h, w = once(case, arm)
                    runs.append({"holds": h, "witness": w})
                except Exception as e:  # noqa: BLE001
                    runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            res["cases"][case][arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                                       "errors": sum(r["holds"] is None for r in runs), "runs": runs}
        c = res["cases"][case]
        c["pass"] = c["fenced"]["holds"] == R and c["control"]["violated"] == R
        ok = ok and c["pass"]
    res["pass"] = ok
    res["not_run"] = "stale replica copy (single node, 0 replicas): offline only, tests/test_es_verify.py"
    print("H3", "PASS" if ok else "FAIL", json.dumps({c: (v["fenced"]["holds"], v["control"]["violated"],
                                                          v["fenced"]["errors"] + v["control"]["errors"]) for c, v in res["cases"].items()}))
    write_result("h_d4_strict", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
