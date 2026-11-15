import json, sys
sys.path.insert(0, ".")
from memsys.esc import ES
es = ES()
for i in ("sys-probe-a-g1", "sys-probe-a-g2"): es.req("DELETE", f"/{i}", ok=None)
es.req("PUT", "/sys-probe-a-g1", {"aliases": {"sys-probe-w": {}, "sys-probe-r": {}}})
es.req("PUT", "/sys-probe-a-g2")
print(es.req("POST", "/_aliases", {"actions": [{"add": {"index": "sys-probe-a-g2", "alias": "sys-probe-w"}}, {"add": {"index": "sys-probe-a-g2", "alias": "sys-probe-r"}}, {"remove_index": {"index": "sys-probe-a-g1"}}]}, ok=None))
print(es.req("GET", "/_alias/sys-probe-*", ok=None))
r = es.bulk([{"index": {"_index": "sys-probe-nonexist", "_id": "1"}}, {"t": "x"}], require_alias=True)
print(json.dumps(r)[:400])
for i in ("sys-probe-a-g1", "sys-probe-a-g2"): es.req("DELETE", f"/{i}", ok=None)
