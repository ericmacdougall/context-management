"""Amendment H1: Elasticsearch 9.5.6 security probe (Basic licence, security on). Decides whether per-tenant, per-epoch
writer API keys minted by a broker can be enforced. A probe, not a PASS of D6.
Amendment H1a (after the H1 result at 606f457): PA adds the explicitly empty role descriptor; PD invalidates with
owner: true; every non-2xx key request records its error.type (never its text); a row whose request was rejected
before it reached the behaviour under test is INVALID-REQUEST, distinct from a behavioural FAIL.

Needs this stream's node with security on (python -m wp.es_security enable) and secrets/es_elastic_password.
The superuser creates the broker role and native user; every probe key is minted by the broker. Key VALUES stay in
memory; the result carries key ids only and the harness asserts that no key value occurs in it. Cleanup invalidates
every probe key and deletes the probe user, role and indices. usage: python -m exp.h_probe_es [R] [R_PD]"""
import base64
import json
import os
import secrets
import sys
import time

from exp.common import write_result
from wp import config, families as F
from wp.es import ES
from wp.es_keys import _superuser_header

P = config.INDEX_PREFIX
TA, TB = "hpa", "hpb"
USER, ROLE = "wp16096_broker", "wp16096_broker_role"
BROKER_PRIVS = ["manage", "create_index", "create_doc", "index", "read", "view_index_metadata"]
PWFILE = config.SECRETS / "es_broker_password"
_VALUES = []           # every key value and the broker password, to assert they never reach the result
_KEY_IDS = []


def base(t):  return F.base_index(t, "events")
def rd(t):    return F.read_alias(t, "events")
def wa(t, e): return F.write_alias(t, "events", e)
def xa(t):    return F.erase_alias(t, "events")


def client(auth):
    es = ES()
    es.auth = auth
    return es


def su():
    return client(_superuser_header())


def broker():
    pw = PWFILE.read_text().strip()
    h = "Basic " + base64.b64encode(f"{USER}:{pw}".encode()).decode()
    del pw
    return client(h)


def etype(j):
    """The error type of a non-2xx reply (never its reason text, which may echo a request value)."""
    return ((j.get("error") or {}).get("type") if isinstance(j, dict) and isinstance(j.get("error"), dict) else None)


ERRORS = {}            # H1a: error.type of every non-2xx key request, by key name (types only, never values)


def mint(by, name, descriptors=None, ok=(200,)):
    body = {"name": f"wp16096-hprobe-{name}", "metadata": {"stream": "goodqa-16096", "probe": "H1"}}
    if descriptors is not None:
        body["role_descriptors"] = descriptors
    s, j = by.req("POST", "/_security/api_key", body, ok=None)
    if s not in ok:
        ERRORS[name] = {"status": s, "error_type": etype(j)}
        return s, None, None
    _VALUES.append(j["encoded"])
    _KEY_IDS.append(j["id"])
    return s, j["id"], client("ApiKey " + j["encoded"])


def idx(names, privs):
    return {"names": names, "privileges": privs}


def writer_descriptors(t, e, extra=()):
    return {"w": {"indices": [idx([wa(t, e)], ["create_doc", "index"]), idx([rd(t)], ["read", "view_index_metadata"]),
                              *extra]}}


def bulk(es, target, require_alias=True, doc_id=None):
    """Status of a one-item bulk: the item status, or the request status when the request itself is refused."""
    doc_id = doc_id or secrets.token_hex(8)
    nd = json.dumps({"index": {"_index": target, "_id": doc_id}}) + "\n" + json.dumps({"event_id": doc_id, "content": "probe"}) + "\n"
    params = {"require_alias": True} if require_alias else {}
    s, j = es.req("POST", "/_bulk", ndjson=nd, ok=None, **params)
    if s != 200:
        return s
    (_, r), = j["items"][0].items()
    return r.get("status", 500)


def search(es, target):
    return es.req("POST", f"/{target}/_search", {"size": 0}, ok=None)[0]


def aliases(es, actions):
    s, j = es.req("POST", "/_aliases", {"actions": actions}, ok=None)
    return 500 if s == 200 and isinstance(j, dict) and j.get("errors") else s


def allowed(s):
    return 200 <= s < 300


def setup():
    s = su()
    lic = s.req("GET", "/_license")[1]["license"]
    if lic["type"] != "basic":
        raise SystemExit(f"refusing: licence is {lic['type']}, the probe must run on basic")
    s.req("PUT", f"/_security/role/{ROLE}", {"cluster": ["manage_own_api_key", "monitor"],
                                              "indices": [idx([f"{P}*"], BROKER_PRIVS)]})
    pw = secrets.token_urlsafe(32)
    fd = os.open(PWFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pw.encode())
    finally:
        os.close(fd)
    _VALUES.append(pw)
    s.req("PUT", f"/_security/user/{USER}", {"password": pw, "roles": [ROLE]})
    del pw
    for t in (TA, TB):
        s.req("DELETE", f"/{base(t)}", ok=(200, 404))
        s.req("PUT", f"/{base(t)}", {"settings": F.SETTINGS, "mappings": F.FAMILIES["events"]["mappings"],
                                     "aliases": {rd(t): {"is_write_index": False}, xa(t): {"is_write_index": True},
                                                 wa(t, 1): {"is_write_index": True}}})
    return lic["type"]


def ensure_w1(t=TA):
    """Restore the probe layout: exactly -w1 as A's write alias (superuser)."""
    s = su()
    have = set(s.alias_map(base(t)).get(base(t), set()))
    acts = [{"remove": {"index": base(t), "alias": a}} for a in have if a.startswith(f"{P}{t}-events-w") and a != wa(t, 1)]
    if wa(t, 1) not in have:
        acts.append({"add": {"index": base(t), "alias": wa(t, 1), "is_write_index": True}})
    if acts:
        s.aliases(acts)


INVALID = "invalid_request"   # H1a: a request rejected before it reached the behaviour under test (not a FAIL)


def status(holds, invalid=False):
    return INVALID if invalid else ("holds" if holds else "fail")


def case_pa(r):
    """H1a: the documented form for a key derived from an API key is an EXPLICITLY EMPTY role descriptor
    (role_descriptors: {}); H1 sent only an omitted descriptor and one with privileges (both refused with 400, so no
    derived key existed to test). Now: the privileged request must be refused; the empty form must be created (if it
    is refused the row is INVALID-REQUEST) and its write through A -w1 refused. The omitted form is recorded."""
    b = broker()
    out = []
    for i in range(r):
        sb, kid, kb = mint(b, f"pa-broker-{i}")                     # broker's own key: no descriptors (snapshot)
        if kb is None:
            out.append({"run": i, "broker_key_status": sb, "holds": False, "status": INVALID})
            continue
        s_desc, _, _ = mint(kb, f"pa-desc-{i}", writer_descriptors(TA, 1), ok=(200,))
        s_nodesc, _, _ = mint(kb, f"pa-nodesc-{i}")
        s_empty, kid0, k0 = mint(kb, f"pa-empty-{i}", {})
        w = bulk(k0, wa(TA, 1)) if k0 else None
        holds = (not allowed(s_desc)) and (k0 is not None) and (w is not None and not allowed(w))
        out.append({"run": i, "broker_key_id": kid, "with_descriptors_status": s_desc, "without_descriptors_status": s_nodesc,
                    "empty_descriptor_status": s_empty, "derived_key_id": kid0, "derived_write_status": w,
                    "holds": holds, "status": status(holds, invalid=allowed(s_desc) is False and k0 is None)})
    return out


def case_pb(r):
    b = broker()
    out = []
    for i in range(r):
        sk, kid, k = mint(b, f"pb-{i}", writer_descriptors(TA, 1))
        if k is None:
            out.append({"run": i, "mint_status": sk, "holds": False, "status": INVALID})
            continue
        row = {"run": i, "key_id": kid,
               "PB1": bulk(k, wa(TA, 1)), "PB2": bulk(k, base(TA), require_alias=False), "PB3": bulk(k, wa(TB, 1)),
               "PB4": bulk(k, xa(TA)), "PB5": search(k, rd(TA)), "PB6": search(k, base(TA)), "PB7": search(k, rd(TB)),
               "PB8": bulk(k, wa(TA, 9), require_alias=False), "PB9": k.req("DELETE", f"/{base(TA)}", ok=None)[0]}
        row["holds"] = allowed(row["PB1"]) and allowed(row["PB5"]) and not any(
            allowed(row[c]) for c in ("PB2", "PB3", "PB4", "PB6", "PB7", "PB8", "PB9"))
        row["status"] = status(row["holds"])
        out.append(row)
    return out


def _add_remove(k, t, alias):
    a = aliases(k, [{"add": {"index": base(t), "alias": alias}}])
    rm = None
    if allowed(a):
        rm = aliases(k, [{"remove": {"index": base(t), "alias": alias, "must_exist": True}}])
    su().req("POST", "/_aliases", {"actions": [{"remove": {"index": base(t), "alias": alias}}]}, ok=None)   # restore
    return a, rm


def case_pc(r):
    b = broker()
    out = []
    for i in range(r):
        _, k1id, k1 = mint(b, f"pc1-{i}", writer_descriptors(TA, 1))
        _, k2id, k2 = mint(b, f"pc2-{i}", {"m": {"indices": [idx([base(TA)], ["manage"])]}})
        _, k3id, k3 = mint(b, f"pc3-{i}", {"m": {"indices": [idx([wa(TA, 2)], ["manage"])]}})
        _, k4id, k4 = mint(b, f"pc4-{i}", {"m": {"indices": [idx([base(TA), wa(TA, 2)], ["manage"])]}})
        _, k5id, k5 = mint(b, f"pc5-{i}", {"m": {"indices": [idx([base(TA), wa(TA, 1)], ["manage"])]}})
        row = {"run": i, "key_ids": [k1id, k2id, k3id, k4id, k5id]}
        if None in (k1, k2, k3, k4, k5):
            ensure_w1()
            out.append(dict(row, holds=False, status=INVALID))
            continue
        for c, k in (("PC1", k1), ("PC2", k2), ("PC3", k3), ("PC4", k4)):
            add, rm = _add_remove(k, TA, wa(TA, 2))
            row[c] = {"add": add, "remove": rm}
        # PC5: the broker removes -w1; a key with manage on the base index and on -w1 tries to put it back
        rm_by_broker = aliases(b, [{"remove": {"index": base(TA), "alias": wa(TA, 1), "must_exist": True}}])
        readd = aliases(k5, [{"add": {"index": base(TA), "alias": wa(TA, 1), "is_write_index": True}}])
        ensure_w1()
        row["PC5"] = {"broker_remove": rm_by_broker, "readd": readd}
        row["holds"] = (not allowed(row["PC1"]["add"])) and allowed(row["PC4"]["add"]) and allowed(row["PC4"]["remove"] or 0) \
            and allowed(rm_by_broker)
        row["status"] = status(row["holds"])
        out.append(row)
    return out


def case_pd(r, retry_s=0.01, window_s=10.0):
    b = broker()
    out = []
    for i in range(r):
        sk, kid, k = mint(b, f"pd-{i}", writer_descriptors(TA, 1))
        if k is None:
            out.append({"run": i, "mint_status": sk, "accepted_after_invalidate": 0, "first_refusal_s": None,
                        "holds": False, "status": INVALID})
            continue
        before = bulk(k, wa(TA, 1))
        # H1a: a caller holding only manage_own_api_key invalidates its own keys with owner: true (H1 sent ids only and
        # got 403 on every run, so revocation was never exercised)
        s, j = b.req("DELETE", "/_security/api_key", {"ids": [kid], "owner": True}, ok=None)
        inv_err = None if allowed(s) else etype(j)
        t0 = time.monotonic()
        accepted, first_refusal, last = 0, None, None
        while time.monotonic() - t0 < window_s:
            last = bulk(k, wa(TA, 1))
            if allowed(last):
                accepted += 1
            else:
                first_refusal = round(time.monotonic() - t0, 4)
                break
            time.sleep(retry_s)
        holds = allowed(before) and allowed(s) and accepted == 0 and first_refusal is not None
        out.append({"run": i, "key_id": kid, "write_before": before, "invalidate_status": s, "invalidate_error_type": inv_err,
                    "accepted_after_invalidate": accepted, "first_refusal_s": first_refusal, "refusal_status": last,
                    "holds": holds, "status": status(holds, invalid=not allowed(s))})
    return out


def case_pe(r):
    b = broker()
    out = []
    for i in range(r):
        ensure_w1()
        sk, kid, k = mint(b, f"pe-{i}", writer_descriptors(TA, 1))
        if k is None:
            out.append({"run": i, "mint_status": sk, "holds": False, "status": INVALID})
            continue
        before = bulk(k, wa(TA, 1))
        cas = aliases(b, [{"remove": {"index": base(TA), "alias": wa(TA, 1), "must_exist": True}},
                          {"add": {"index": base(TA), "alias": wa(TA, 2), "is_write_index": True}}])
        with_ra = bulk(k, wa(TA, 1), require_alias=True)
        without_ra = bulk(k, wa(TA, 1), require_alias=False)
        ensure_w1()
        holds = allowed(before) and allowed(cas) and not allowed(with_ra) and not allowed(without_ra)
        out.append({"run": i, "key_id": kid, "write_before": before, "broker_cas": cas, "require_alias": with_ra,
                    "no_require_alias": without_ra, "holds": holds, "status": status(holds)})
    return out


def cleanup():
    s = su()
    out = {}
    if _KEY_IDS:
        out["invalidated"] = len(s.req("DELETE", "/_security/api_key", {"ids": _KEY_IDS}, ok=None)[1].get("invalidated_api_keys", []))
    for t in (TA, TB):
        s.req("DELETE", f"/{base(t)}", ok=(200, 404))
    out["user_deleted"] = s.req("DELETE", f"/_security/user/{USER}", ok=(200, 404))[0]
    out["role_deleted"] = s.req("DELETE", f"/_security/role/{ROLE}", ok=(200, 404))[0]
    PWFILE.unlink(missing_ok=True)
    return out


def main(r=3, r_pd=20):
    licence = setup()
    try:
        res = {"licence": licence, "pa": case_pa(r), "pb": case_pb(r), "pc": case_pc(r), "pd": case_pd(r_pd), "pe": case_pe(r)}
    finally:
        res_cleanup = cleanup()
    pd = res["pd"]
    verdict = {c: all(x["holds"] for x in res[c]) for c in ("pa", "pb", "pc", "pd", "pe")}
    # H1a: per case, fail if any row failed behaviourally; else invalid_request if any row's request never reached
    # the behaviour; else holds. Decided from statuses and error types only.
    case_status = {c: ("fail" if any(x["status"] == "fail" for x in res[c]) else
                       INVALID if any(x["status"] == INVALID for x in res[c]) else "holds") for c in verdict}
    refusals = [x["first_refusal_s"] for x in pd if x.get("first_refusal_s") is not None]
    res.update(cleanup=res_cleanup, verdict=verdict, case_status=case_status, feasible=all(verdict.values()),
               failed_cases=[c for c, ok in verdict.items() if not ok],
               invalid_request_cases=[c for c, s in case_status.items() if s == INVALID],
               key_request_errors=ERRORS,
               pd_max_first_refusal_s=max(refusals) if refusals else None,      # H1a: null when nothing was refused
               pd_accepted_after_invalidate_total=sum(x["accepted_after_invalidate"] for x in pd),
               scope="single node, Basic licence, security on, HTTP without TLS on loopback; cross-node API-key cache "
                     "invalidation is not shown")
    raw = json.dumps(res, default=str)
    assert not any(v and v in raw for v in _VALUES), "a key value or password reached the result"
    write_result("h_probe_es", res)
    print("h_probe_es feasible", res["feasible"], "failed", res["failed_cases"], "case_status", json.dumps(case_status),
          "pd_max_first_refusal_s", res["pd_max_first_refusal_s"])
    return 0


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:]]
    sys.exit(main(*a))
