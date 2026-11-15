"""Delete this stream's own Elasticsearch indices (prefix wp16096- only) on this stream's own cluster."""
from wp import config
from wp.es import ES

assert config.INDEX_PREFIX == "wp16096-" and config.ES_URL == "http://127.0.0.1:19260"
es = ES()
names = es.indices(config.INDEX_PREFIX + "*")
for i in range(0, len(names), 50):
    es.req("DELETE", "/" + ",".join(names[i:i + 50]), ok=(200, 404))
print("deleted", len(names), "indices with prefix", config.INDEX_PREFIX)
