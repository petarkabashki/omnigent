#!/usr/bin/env bash
# Run this checkout as a local stack: server + host + Electron desktop shell.
#
# Usage: scripts/local-desktop.sh [--no-electron] [--no-sync] [--rebuild-web] [--port N]
#        scripts/local-desktop.sh --update-web | --restart-server [--no-sync] [--port N]
#   --no-electron     start only server + host (use the browser at http://localhost:PORT)
#   --no-sync         skip `uv sync`
#   --rebuild-web     force a web UI build even if it looks current
#   --update-web      build and deploy the web UI into the running stack, then exit;
#                     reload the app to see it, nothing else restarts
#   --restart-server  restart only the running server, then exit; the host, its
#                     sessions and Electron keep running and reconnect
#   --port N          server port (default 6767)
#
# Uses your real ~/.omnigent state (sessions in chat.db). The database is backed
# up once per day before the server starts, since this build may migrate it.
# Ctrl-C (or quitting Electron) stops the server and host, which ends sessions.
# The shadow router log is on (rules backend, free); set OMNIGENT_ROUTER_SHADOW= to turn it off.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port=6767
run_electron=1
run_sync=1
force_web=0
update_web=0
restart_server=0
pids=()

while [ $# -gt 0 ]; do
    case "$1" in
        --no-electron) run_electron=0 ;;
        --no-sync) run_sync=0 ;;
        --rebuild-web) force_web=1 ;;
        --update-web) update_web=1 ;;
        --restart-server) restart_server=1 ;;
        --port)
            port="${2:?--port needs a value}"
            shift
            ;;
        -h | --help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
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

# The listener on our port, whichever run started it, so a server restarted
# with --restart-server is still found and stopped.
server_pid() {
    local pid
    pid="$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1)" || true
    if [ -n "$pid" ] && ps -o args= -p "$pid" | grep -q 'omnigent server'; then
        echo "$pid"
    fi
}

stop_server() {
    local pid
    pid="$(server_pid)"
    [ -n "$pid" ] || return 0
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 120); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.5
    done
    echo "Server ${pid} did not stop within 60s; see ${log_dir}/server.log" >&2
    return 1
}

# Assets land before index.html, and earlier builds' assets stay a while, so an
# open window can still load its lazy chunks while a new build goes live.
build_web() {
    local dest build_dir
    dest="$repo_root/omnigent/server/static/web-ui"
    build_dir="$(mktemp -d)"
    (cd web && pnpm install --frozen-lockfile &&
        pnpm exec vite build --outDir "$build_dir" --emptyOutDir)
    mkdir -p "$dest"
    mv "$build_dir/index.html" "$dest/.index.html.new"
    cp -a "$build_dir"/. "$dest"/
    mv -f "$dest/.index.html.new" "$dest/index.html"
    find "$dest/assets" -type f -mtime +7 -delete
    rm -rf "$build_dir"
}

backup_db() {
    [ -f "${data_dir}/chat.db" ] || return 0
    local backup="${data_dir}/chat.db.bak-$(date +%F)"
    [ ! -f "$backup" ] || return 0
    step "Backing up chat.db -> ${backup}"
    # sqlite's online backup copies a consistent snapshot, including the WAL.
    uv run --no-sync python -c \
        "import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()" \
        "${data_dir}/chat.db" "$backup"
}

start_server() {
    step "Starting server on ${server_url} (log: ${log_dir}/server.log)"
    OMNIGENT_ROUTER_SHADOW="${OMNIGENT_ROUTER_SHADOW-rules}" \
        "$@" uv run --no-sync omnigent server --port "$port" >>"${log_dir}/server.log" 2>&1 &
    server_launcher=$!
    pids+=("$server_launcher")
    for _ in $(seq 1 120); do
        healthy && return 0
        if ! kill -0 "$server_launcher" 2>/dev/null; then
            echo "Server exited during startup; last log lines:" >&2
            tail -20 "${log_dir}/server.log" >&2
            return 1
        fi
        sleep 0.5
    done
    echo "Server did not become healthy within 60s; see ${log_dir}/server.log" >&2
    return 1
}

web_is_stale() {
    local web_index="omnigent/server/static/web-ui/index.html"
    [ "$force_web" -eq 1 ] || [ ! -f "$web_index" ] ||
        [ -n "$(find web/src web/index.html web/package.json -newer "$web_index" -print -quit 2>/dev/null)" ]
}

if [ "$update_web" -eq 1 ]; then
    step "Building and deploying the web UI"
    build_web
    echo "Deployed. Reload the app to see it."
    exit 0
fi

if [ "$restart_server" -eq 1 ]; then
    if [ -z "$(server_pid)" ]; then
        echo "No omnigent server is listening on ${server_url}." >&2
        exit 1
    fi
    if [ "$run_sync" -eq 1 ]; then
        step "Syncing Python dependencies"
        uv sync --extra all --group dev
    fi
    if web_is_stale; then
        step "Building and deploying the web UI"
        build_web
    fi
    backup_db
    step "Stopping the server"
    stop_server
    start_server nohup
    disown "$server_launcher"
    echo "Server restarted. The host and its sessions reconnect on their own."
    exit 0
fi

if healthy; then
    echo "A server is already answering on ${server_url}." >&2
    echo "Stop it first (e.g. 'omnigent server stop') or pass --port." >&2
    exit 1
fi

if [ "$run_sync" -eq 1 ]; then
    step "Syncing Python dependencies"
    uv sync --extra all --group dev
fi

if web_is_stale; then
    step "Building the web UI"
    build_web
else
    echo "Web UI is up to date (use --rebuild-web to force)."
fi

backup_db

cleanup() {
    trap - EXIT INT TERM
    if [ ${#pids[@]} -gt 0 ]; then
        step "Stopping server and host"
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
        stop_server || true
    fi
}
trap cleanup EXIT INT TERM

start_server env

step "Registering this machine as a host (log: ${log_dir}/host.log)"
uv run --no-sync omnigent host --server "$server_url" >"${log_dir}/host.log" 2>&1 &
pids+=($!)

if [ "$run_electron" -eq 1 ]; then
    step "Launching Electron - on first run, enter ${server_url} and Connect"
    (cd web/electron && { [ -d node_modules ] || pnpm install; } && pnpm start)
else
    step "Running. Open ${server_url} in a browser; Ctrl-C to stop."
    # Wait on the host: the server may be restarted underneath this script.
    wait "${pids[1]}"
fi
