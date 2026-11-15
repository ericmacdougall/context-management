import sys; from esc import *
out = {}
out["root"] = req("GET", "/")[1]
out["license"] = req("GET", "/_license")[1]
out["watermarks"] = req("GET", "/_cluster/settings", include_defaults="true", filter_path="*.cluster.routing.allocation.disk.watermark*")[1]
c, j, _ = req("PUT", "/e0-test", {"settings": {"number_of_replicas": 0}}); out["create"] = j
req("POST", "/e0-test/_doc/1", {"a": 1}, refresh="true")
out["health"] = req("GET", "/_cluster/health/e0-test", wait_for_status="green", timeout="30s")[1]
out["allocation"] = req("GET", "/_cat/allocation", format="json")[1]
out["blocks"] = req("GET", "/e0-test/_settings", filter_path="*.settings.index.blocks")[1]
out["write_after"] = req("POST", "/e0-test/_doc/2", {"a": 2}, refresh="true")[1].get("result")
req("DELETE", "/e0-test")
save("e0_baseline_" + sys.argv[1] + ".json", out); print(json.dumps(out, indent=1)[:2500])
