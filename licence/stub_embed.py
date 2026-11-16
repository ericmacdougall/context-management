"""Deterministic stub embedding server (NO model). POST {"input": [text,...]} -> {"data":[{"embedding":[8 floats]},...]}.
Bound to the docker bridge address so the ES container can reach it; counts requests in a file for witness."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(__file__))
from lib import embed

COUNT = {"n": 0}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        inp = body.get("input", [])
        inp = [inp] if isinstance(inp, str) else inp
        COUNT["n"] += len(inp)
        out = json.dumps({"data": [{"embedding": embed(t)} for t in inp], "count_total": COUNT["n"]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        out = json.dumps(COUNT).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((sys.argv[1] if len(sys.argv) > 1 else "172.17.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 19271), H).serve_forever()
