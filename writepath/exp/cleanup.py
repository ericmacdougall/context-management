"""Delete this stream's own Elasticsearch indices (prefix wp16096- only) on this stream's own cluster, after printing
how many indices and shards they held and the node's shard headroom (item 5 of the 4960f50 round: run_all calls this
between experiments, so no single experiment's tenants count against the next one's shard budget).
usage: python -m exp.cleanup [label]"""
import sys

from wp import config
from wp.es import ES

assert config.INDEX_PREFIX == "wp16096-" and config.ES_CONTAINER.startswith(config.OWN_PREFIX)   # own node only (conf)
es = ES()
names = es.indices(config.INDEX_PREFIX + "*")
s, sh = es.req("GET", f"/_cat/shards/{config.INDEX_PREFIX}*", ok=(200, 404), format="json", h="index", expand_wildcards="all")
own_shards = len(sh) if s == 200 else 0
_, allsh = es.req("GET", "/_cat/shards", format="json", h="index")
_, st = es.req("GET", "/_cluster/settings", include_defaults="true", flat_settings="true")
lim = {**st.get("defaults", {}), **st.get("persistent", {}), **st.get("transient", {})}.get("cluster.max_shards_per_node", "1000")
tenants = {n[len(config.INDEX_PREFIX):].split("-")[0] for n in names}
print("cleanup", sys.argv[1] if len(sys.argv) > 1 else "", "indices", len(names), "shards", own_shards, "tenants", len(tenants),
      "node_shards_total", len(allsh), "max_shards_per_node", lim)
for i in range(0, len(names), 50):
    es.req("DELETE", "/" + ",".join(names[i:i + 50]), ok=(200, 404))
print("deleted", len(names), "indices with prefix", config.INDEX_PREFIX)
