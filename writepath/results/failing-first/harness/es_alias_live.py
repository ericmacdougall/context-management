"""Live check on the lab Elasticsearch (review C F5, F9), unstamped. A throwaway tenant's families are created, one
family is polluted (-w4 the write index, -w5 present but NOT the write index), and families.fence_es(epoch 5) must
converge every family to exactly one -w5 write alias. That needs the server to accept a remove and an add of the same
alias name on the same index in one _aliases request. Then one per-copy search with allow_partial_search_results=false
must be accepted and complete. The tenant's indices are deleted afterwards. Prints alias names and counts only.
usage (from WP_HOME/src): ../venv/bin/python results/failing-first/harness/es_alias_live.py"""
import sys
import uuid

sys.path.insert(0, ".")
from wp import es_verify as EV, families as F  # noqa: E402
from wp.es import ES  # noqa: E402


def walias(es, t):
    return sorted((i, a, w) for i, m in es.alias_meta(name=f"{F.P}{t}-*-w*").items() for a, w in m.items())


es, t = ES(), "rc" + uuid.uuid4().hex[:8]
ok = False
try:
    for f in F.FAMILIES:
        F.ensure_family(es, t, f)
    f0 = next(iter(F.FAMILIES))
    base = F.base_index(t, f0)
    es.aliases([{"add": {"index": base, "alias": F.write_alias(t, f0, 4), "is_write_index": True}},
                {"add": {"index": base, "alias": F.write_alias(t, f0, 5), "is_write_index": False}}])
    print("before", walias(es, t))
    F.fence_es(es, t, 5)
    after = walias(es, t)
    want = sorted((F.base_index(t, f), F.write_alias(t, f, 5), True) for f in F.FAMILIES)
    print("after ", after)
    f5 = after == want
    print("F5_converged_to_one_write_alias", f5)
    r = EV.check_page(es, t, f0, ["absent-id"], EV.copies(es, f"{F.P}{t}-{f0}-*"))
    f9 = r["es:copy_unverified"] == 0 and r["es:placeholder_missing"] == 1
    print("F9_complete_search_with_param", f9, r)
    ok = f5 and f9
finally:
    for f in F.FAMILIES:
        es.req("DELETE", f"/{F.base_index(t, f)}", ok=(200, 404))
    print("cleanup: deleted the throwaway tenant's indices")
sys.exit(0 if ok else 1)
