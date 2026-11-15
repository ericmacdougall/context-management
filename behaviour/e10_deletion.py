import subprocess, uuid
from esc import *
MARK = "ZXQMARKER" + uuid.uuid4().hex[:12]   # unique, ascii, long enough to find in raw bytes
IDX = "e10-del"
def grep_files(label):
    # read-only: list files under our own index dir whose bytes contain the marker
    uuid_ = req("GET", f"/{IDX}/_settings", filter_path="*.settings.index.uuid")[1][IDX]["settings"]["index"]["uuid"]
    cmd = ["docker", "exec", "emlab-es-16096", "sh", "-c", f"grep -rl --binary-files=text '{MARK}' /usr/share/elasticsearch/data/indices/{uuid_}/ 2>/dev/null; true"]
    files = [f.split(uuid_ + "/")[1] for f in subprocess.run(cmd, capture_output=True, text=True).stdout.split() if f]
    allf = subprocess.run(["docker", "exec", "emlab-es-16096", "sh", "-c", f"ls /usr/share/elasticsearch/data/indices/{uuid_}/0/index | tr '\\n' ' '; echo; ls /usr/share/elasticsearch/data/indices/{uuid_}/0/translog | tr '\\n' ' '"], capture_output=True, text=True).stdout
    st = req("GET", f"/{IDX}/_stats/docs,segments,translog")[1]["_all"]["primaries"]
    row = {"step": label, "files_containing_marker": sorted(files), "docs": st["docs"], "segments_count": st["segments"]["count"], "translog_ops": st["translog"]["operations"], "translog_uncommitted_ops": st["translog"]["uncommitted_operations"], "listing": allf.strip()}
    print(json.dumps(row), flush=True); return row
req("DELETE", f"/{IDX}")
req("PUT", f"/{IDX}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1", "index.codec": "default"}, "mappings": {"properties": {"body": {"type": "text"}, "k": {"type": "keyword"}}}})
nd = "".join(json.dumps({"index": {"_id": f"d{n}"}}) + "\n" + json.dumps({"body": f"filler text number {n} " * 5, "k": f"k{n}"}) + "\n" for n in range(40))
nd += json.dumps({"index": {"_id": "secret"}}) + "\n" + json.dumps({"body": f"private note {MARK} end", "k": MARK}) + "\n"
req("POST", f"/{IDX}/_bulk", ndjson=nd); req("POST", f"/{IDX}/_refresh")
rows = []
rows.append(grep_files("1 indexed+refreshed, not flushed"))
req("POST", f"/{IDX}/_flush", force="true", wait_if_ongoing="true"); rows.append(grep_files("2 after flush"))
out = {"marker_note": "unique per-run marker ZXQMARKER+12hex, present in body (text) and k (keyword)", "mark_len": len(MARK)}
out["delete"] = req("DELETE", f"/{IDX}/_doc/secret", refresh="true")[1].get("result")
rows.append(grep_files("3 deleted + refresh (no flush)"))
req("POST", f"/{IDX}/_flush", force="true"); rows.append(grep_files("4 deleted + flush"))
# delete more docs so segment crosses the expunge threshold (default 10%)
for n in range(0, 8): req("DELETE", f"/{IDX}/_doc/d{n}")
req("POST", f"/{IDX}/_refresh"); req("POST", f"/{IDX}/_flush", force="true"); rows.append(grep_files("5 8 more docs deleted + flush (deleted%% ~ 20%%)"))
c, j, _ = req("POST", f"/{IDX}/_forcemerge", only_expunge_deletes="true"); out["expunge_merge"] = (c, j)
req("POST", f"/{IDX}/_refresh"); rows.append(grep_files("6 after forcemerge only_expunge_deletes"))
req("POST", f"/{IDX}/_flush", force="true"); rows.append(grep_files("7 after expunge + flush"))
c, j, _ = req("POST", f"/{IDX}/_forcemerge", max_num_segments=1); out["full_merge"] = (c, j)
req("POST", f"/{IDX}/_flush", force="true"); rows.append(grep_files("8 after forcemerge max_num_segments=1 + flush"))
out["steps"] = rows; out["stats_docs_final"] = req("GET", f"/{IDX}/_stats/docs")[1]["_all"]["primaries"]["docs"]
save("E10_deletion.json", out)
