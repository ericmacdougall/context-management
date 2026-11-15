"""Child process: append a user_typed amendment to a task session, then die abruptly at the named point."""
import json, sys
from evolution import evo, taskview
tenant, session, point, item = sys.argv[1:5]
conn = evo.connect()
taskview.append(conn, tenant, session, "task_amended", {"amendment_id": "CRASH-" + point, "ops": [{"op": "change", "id": item, "text": "text after crash amendment"}]}, n=9999)
taskview.kill("after_append", point)
taskview.update_view(conn, tenant, session, crash_at=point)
