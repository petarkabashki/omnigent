---
name: omnigent-session-roles
description: Name an Omnigent session with a role handle, release that handle, and restart the local desktop stack without stopping the wrong server. Use when setting or clearing a session alias, when a CLI command must target the hub on port 6767, or when the desktop app must be restarted to load a new web bundle.
---

# Omnigent session roles and the local desktop stack

A role handle is `project/Role` for a session filed in a project, or `/Role`
when the session is unfiled. The header, the session switcher, and the sidebar
row show the handle of the session that holds the name.

## Name a session

The session id is the last segment of its URL (`/c/<id>`), a 32-character hex
id. Outside a session pane, pass it explicitly:

```bash
omnigent session alias Scratch --id SESSION_ID
```

Inside the session's pane the runner sets the session id, so omit `--id`:

```bash
omnigent session alias Scratch
```

Release the handle from the session menu (**Release role**), or:

```bash
omnigent session alias --clear --id SESSION_ID
```

Taking a name is `PUT /v1/sessions/{id}/alias`. Releasing it is
`DELETE /v1/sessions/{id}/alias`. A name another live session holds returns
409; do not take it over.

## Point the CLI at the hub

`~/.omnigent/config.yaml` needs `server: http://127.0.0.1:6767` before a CLI
command. Without that line, the CLI can start a second server and migrate the
live database.

## Restart the desktop stack

The stack is `scripts/local-desktop.sh`. Its process group owns the server,
the host, and Electron. Killing only the script pid leaves the server up,
because Electron still holds the group. `omnigent server stop` follows
`~/.omnigent/local_server.pid` and can stop a different server.

Find the script and its group, then signal the group:

```bash
ps -eo pid,pgid,args | grep '[s]cripts/local-desktop.sh'
kill -- -PGID
```

Use the `pgid` from that line. Start the stack again the same way it was
running (`--no-sync` skips `uv sync`). The script rebuilds the web UI when
`web/src` is newer than the served bundle; the running server keeps the old
bundle until this restart.
