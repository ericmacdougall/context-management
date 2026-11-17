#!/bin/sh
# Run on t2-dev inside ONE synced copy of the tree:  sh ~/emlab2-16096/policy/runs/<name>/src/hostrun.sh m|t|all|v [args...]
#   t <unittest targets...>   unit tests      all [run_all args]   the experiments + verify      m <module>   one module      v [args]   verify
# The Postgres environment is exported as FILE PATHS to 0600 files only (never a secret value). The database is the policy owner's own
# container emlab2-policy-pg (postgres:16, 127.0.0.1:25610, --memory 4g); venv ~/emlab2-16096/policy/venv (psycopg 3.2.3).
SRC=$(cd "$(dirname "$0")" && pwd)
B="$HOME/emlab2-16096/policy"
case "$SRC" in "$B"/runs/*/src) ;; *) echo "hostrun.sh must run from $B/runs/<name>/src"; exit 2;; esac
cd "$SRC" || exit 1
S="$B/secret"
export POL_PG_PORT=25610 POL_PG_SUPER_PASSFILE="$S/pgpass_super" POL_PG_APP_PASSWORD_FILE="$S/pg_app" POL_PG_APP_PASSFILE="$S/pgpass_app"
export POL_PG_IDP_PASSWORD_FILE="$S/pg_idp" POL_PG_IDP_PASSFILE="$S/pgpass_idp"
export PY="$B/venv/bin/python"
what="$1"; shift
case "$what" in
  m) exec "$PY" -W ignore -m "$@" ;;
  t) exec "$PY" -m unittest "$@" ;;
  all) exec sh run_all.sh "$@" ;;
  v) exec "$PY" -m verify "$@" ;;
  *) echo "usage: hostrun.sh m|t|all|v ..."; exit 2 ;;
esac
