#!/bin/sh
# Live item L8: run runkit/clearance.py daemon() of the committed tree on t2-dev's docker CLI, read-only (docker info,
# docker context show). The function's source is sent over stdin; nothing is written on the host. Prints the ID's
# shape and a SHA-256 prefix, never the ID itself.
# usage (from writepath/): sh <this> <commit>
set -u
C=$1
SRC=$(git show "$C:writepath/runkit/clearance.py" | sed -n '/^def daemon():/,/^def _same_daemon/p' | sed '$d')
{
  printf 'import hashlib, os, re, subprocess\nclass Refused(Exception):\n    pass\n'
  printf '%s\n' "$SRC"
  cat <<'EOF'
try:
    d = daemon()
except Refused as e:
    print("daemon() refused:", e)
    raise SystemExit(1)
print("daemon() ok: id shape", "uuid-like" if re.fullmatch(r"[0-9a-f-]{36}", d["id"]) else "other",
      "len", len(d["id"]), "sha256", hashlib.sha256(d["id"].encode()).hexdigest()[:12],
      "| name set", bool(d["name"]), "| context", d["context"],
      "| DOCKER_HOST set", bool(d["DOCKER_HOST"]), "| DOCKER_CONTEXT set", bool(d["DOCKER_CONTEXT"]))
d2 = daemon()
print("second call equal:", d2 == d)
EOF
} | ssh -F ~/.ssh/config_fleetops t2-dev 'python3 -I -'
echo "exit $?"
echo "blob $(git rev-parse "$C:writepath/runkit/clearance.py")"
