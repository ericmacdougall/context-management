import sys
from esc import *
adm = admin_session(); out = {"license_before": req("GET", "/_license", sess=adm)[1]}
out["trial_status_before"] = req("GET", "/_license/trial_status", sess=adm)[1]
c, j, _ = req("POST", "/_license/start_trial", sess=adm, acknowledge="true"); out["start_trial"] = {"status": c, "body": j}
out["license_after"] = req("GET", "/_license", sess=adm)[1]
out["trial_status_after"] = req("GET", "/_license/trial_status", sess=adm)[1]
save("E_trial_start.json", out); print(json.dumps(out, indent=1))
