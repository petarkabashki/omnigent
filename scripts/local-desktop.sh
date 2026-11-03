#!/usr/bin/env bash
# Run this checkout as a local stack: server + host + Electron desktop shell.
#
# Usage: scripts/local-desktop.sh [--no-electron] [--no-sync] [--rebuild-web] [--port N]
#   --no-electron  start only server + host (use the browser at http://localhost:PORT)
#   --no-sync      skip `uv sync`
#   --rebuild-web  force a web UI build even if it looks current
#   --port N       server port (default 6767)
#
# Uses your real ~/.omnigent state (sessions in chat.db). The database is backed
# up once per day before the server starts, since this build may migrate it.
# Ctrl-C (or quitting Electron) stops the server and host started here.
# The shadow router log is on (rules backend, free); set OMNIGENT_ROUTER_SHADOW= to turn it off.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port=6767
run_electron=1
run_sync=1
force_web=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-electron) run_electron=0 ;;
        --no-sync) run_sync=0 ;;
        --rebuild-web) force_web=1 ;;
        --port)
            port="${2:?--port needs a value}"
            shift
            ;;
        -h | --help)
            sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $1 (see --help)" >&2
            exit 2
            ;;
    esac
    shift
done

server_url="http://localhost:${port}"
data_dir="${OMNIGENT_DATA_DIR:-$HOME/.omnigent}"
log_dir="${data_dir}/logs/local-desktop"
mkdir -p "$log_dir"
cd "$repo_root"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
healthy() { curl -fsS -o /dev/null "${server_url}/health" 2>/dev/null; }

if healthy; then
    echo "A server is already answering on ${server_url}." >&2
    echo "Stop it first (e.g. 'omnigent server stop') or pass --port." >&2
    exit 1
fi

if [ "$run_sync" -eq 1 ]; then
    step "Syncing Python dependencies"
    uv sync --extra all --group dev
fi

web_index="omnigent/server/static/web-ui/index.html"
if [ "$force_web" -eq 1 ] || [ ! -f "$web_index" ] ||
    [ -n "$(find web/src web/index.html web/package.json -newer "$web_index" -print -quit 2>/dev/null)" ]; then
    step "Building the web UI"
    (cd web && pnpm install --frozen-lockfile && pnpm run build)
else
    echo "Web UI is up to date (use --rebuild-web to force)."
fi

if [ -f "${data_dir}/chat.db" ]; then
    backup="${data_dir}/chat.db.bak-$(date +%F)"
    if [ ! -f "$backup" ]; then
        step "Backing up chat.db -> ${backup}"
        # sqlite's online backup copies a consistent snapshot, including the WAL.
        uv run --no-sync python -c \
            "import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst)" \
            "${data_dir}/chat.db" "$backup"
    fi
fi

pids=()
cleanup() {
    trap - EXIT INT TERM
    if [ ${#pids[@]} -gt 0 ]; then
        step "Stopping server and host"
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

step "Starting server on ${server_url} (log: ${log_dir}/server.log)"
OMNIGENT_ROUTER_SHADOW="${OMNIGENT_ROUTER_SHADOW-rules}" \
    uv run --no-sync omnigent server --port "$port" >"${log_dir}/server.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 120); do
    healthy && break
    if ! kill -0 "${pids[0]}" 2>/dev/null; then
        echo "Server exited during startup; last log lines:" >&2
        tail -20 "${log_dir}/server.log" >&2
        exit 1
    fi
    sleep 0.5
done
healthy || {
    echo "Server did not become healthy within 60s; see ${log_dir}/server.log" >&2
    exit 1
}

step "Registering this machine as a host (log: ${log_dir}/host.log)"
uv run --no-sync omnigent host --server "$server_url" >"${log_dir}/host.log" 2>&1 &
pids+=($!)

if [ "$run_electron" -eq 1 ]; then
    step "Launching Electron - on first run, enter ${server_url} and Connect"
    (cd web/electron && { [ -d node_modules ] || pnpm install; } && pnpm start)
else
    step "Running. Open ${server_url} in a browser; Ctrl-C to stop."
    wait "${pids[0]}"
fi
