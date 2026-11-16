"""Exploratory (not a reported result): does soft-delete retention keep an overwritten doc's bytes past
_forcemerge?only_expunge_deletes, and does waiting for the retention lease to advance release them?"""
import time

from wp.es import ES
from exp.registry_marker import grep_files, ES_C

es = ES()
P = "wp16096-probe-xr"
es.req("DELETE", "/" + P, ok=(200, 404))
es.req("PUT", "/" + P, {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "index.merge.policy.expunge_deletes_allowed": 0},
                        "aliases": {P + "-w": {"is_write_index": True}}})
mk = "zqp12r" + str(int(time.time()))
es.bulk_fenced([({"create": {"_index": P + "-w", "_id": "d%d" % i}}, {"content": mk if i == 0 else "f%d" % i}) for i in range(200)])
es.req("POST", "/%s/_refresh" % P); es.req("POST", "/%s/_flush" % P)
es.bulk_fenced([({"index": {"_index": P + "-w", "_id": "d0", "version": 2 ** 62, "version_type": "external"}}, {"erased": True})])
es.req("POST", "/%s/_refresh" % P); es.req("POST", "/%s/_flush" % P)


def rl():
    j = es.req("GET", "/%s/_stats" % P, level="shards")[1]
    sh = j["indices"][P]["shards"]["0"][0]
    return [(x["id"], x["retaining_seq_no"]) for x in sh["retention_leases"]["leases"]], sh["seq_no"]["max_seq_no"]


print("leases/max_seq", rl())
for i in range(4):
    if i:
        time.sleep(35)
    es.req("POST", "/%s/_forcemerge" % P, only_expunge_deletes=True); es.req("POST", "/%s/_flush" % P)
    st = es.req("GET", "/%s/_stats/docs" % P)[1]["_all"]["primaries"]["docs"]
    print(i * 35, st, rl(), "files", grep_files(ES_C, "/usr/share/elasticsearch/data", mk), flush=True)
es.req("DELETE", "/" + P)
