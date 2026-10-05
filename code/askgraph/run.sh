#!/usr/bin/env bash
# askgraph launcher.
#   ./run.sh ui                                   Chainlit chat UI on http://localhost:8000 (login + history)
#   ./run.sh cli [-v] [--user hr|marketing] "q"   one question in the terminal (no q = interactive)
#   ./run.sh test                                 live golden tests (pytest)
# Secrets are read at runtime from private files (see README); nothing secret lives here.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
[[ -x "$PY" ]] || { echo "No venv at $HERE/.venv (see README: Setup)" >&2; exit 1; }
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"

cmd=${1:-}; shift || true
case "$cmd" in
  ui)
    # Chainlit login needs CHAINLIT_AUTH_SECRET (signs the session cookie). It lives outside
    # this folder; created once on first run. Never printed.
    SECRET_FILE="${CHAINLIT_ENV_FILE:-$HOME/.config/semantic-layer/chainlit.env}"
    if [[ ! -f "$SECRET_FILE" ]]; then
      mkdir -p "$(dirname "$SECRET_FILE")"
      ( umask 077
        secret=$("$HERE/.venv/bin/chainlit" create-secret 2>/dev/null | sed -n 's/^CHAINLIT_AUTH_SECRET="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' | tail -1)
        [[ -n "$secret" ]] || { echo "could not generate a Chainlit auth secret" >&2; exit 1; }
        printf "export CHAINLIT_AUTH_SECRET='%s'\n" "$secret" > "$SECRET_FILE" )
      chmod 600 "$SECRET_FILE"
      echo "Created $SECRET_FILE (Chainlit auth secret)" >&2
    fi
    # shellcheck disable=SC1090
    source "$SECRET_FILE"
    exec "$HERE/.venv/bin/chainlit" run app.py --port "${PORT:-8000}" "$@" ;;
  cli)  exec "$PY" cli.py "$@" ;;
  test) exec "$PY" -m pytest -q tests "$@" ;;
  *)    echo "usage: $0 ui | cli [-v] [--user hr|marketing] [question] | test" >&2; exit 1 ;;
esac
