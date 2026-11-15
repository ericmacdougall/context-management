"""Tiny ES client for the behaviour experiments. Reads API key + CA from ~/emlab-16096 on the host (never prints them)."""
import json, os, time, requests
H = os.path.expanduser("~/emlab-16096")
URL = "https://127.0.0.1:19200"
_k = json.load(open(f"{H}/key.json"))["encoded"]
S = requests.Session()
S.verify = f"{H}/http_ca.crt"
S.headers.update({"Authorization": f"ApiKey {_k}", "Content-Type": "application/json"})
RES = os.environ.get("RESDIR", os.path.expanduser("~/emlab-16096/results"))
os.makedirs(RES, exist_ok=True)

def admin_session():
    """Admin (elastic user) session; password read from file, kept in memory only."""
    pw = open(f"{H}/es_password").read().strip()
    s = requests.Session(); s.verify = f"{H}/http_ca.crt"; s.auth = ("elastic", pw)
    s.headers["Content-Type"] = "application/json"
    return s

def req(method, path, body=None, sess=None, ndjson=None, **params):
    s = sess or S
    kw = {"params": params}
    if ndjson is not None:
        kw["data"] = ndjson; kw["headers"] = {"Content-Type": "application/x-ndjson"}
    elif body is not None:
        kw["data"] = json.dumps(body)
    t = time.perf_counter()
    r = s.request(method, URL + path, **kw)
    dt = (time.perf_counter() - t) * 1000
    try: j = r.json()
    except Exception: j = {"_raw": r.text[:2000]}
    return r.status_code, j, dt

def save(name, obj):
    with open(f"{RES}/{name}", "w") as f:
        json.dump(obj, f, indent=1, default=str)
