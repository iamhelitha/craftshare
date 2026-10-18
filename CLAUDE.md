# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local web app (aiohttp backend + one static page) that downloads, configures and runs a vanilla Minecraft server, exposes it through an SSH reverse tunnel to a VPS, and keeps a shared world on that VPS so friends can take turns hosting (one at a time). No tests, linter or build step beyond PyInstaller packaging.

## Commands

- Run: `start.bat` (creates `.venv`, installs `requirements.txt` if `aiohttp` is missing, runs `app.py`, which opens the browser) or `python app.py`.
- Run against throwaway data without opening a browser: `MCMGR_DATA=<tmp> MCMGR_CONFIG=<tmp>/cfg.json MCMGR_NO_BROWSER=1 MCMGR_NO_LAUNCHER=1 python app.py`, then drive `http://127.0.0.1:8765/api/...` with the `X-Token` header (the token is embedded in `/` as `const TOKEN = "..."`). Stop it with `POST /api/shutdown`.
- Friend zip: `python package.py` (sources) or `build.bat` / `python package.py --exe` (PyInstaller one-file exe with `--add-data "web;web"`).

## Architecture

- `app.py`: `Manager` holds all runtime state (server + tunnel `ManagedProcess`es, log ring buffers, hosting flags, current long operation) and `make_app` exposes it as a JSON API. The page polls `GET /api/state?log=N&tlog=N&note=N` every second and receives only new log lines/notifications (sequence numbers). Long operations go through `Manager.run_task` (one at a time; exceptions become notifications). When a flow needs a user decision it calls `await mgr.ask(text, options)`; the question appears in `/api/state`, the page shows a dialog and answers via `POST /api/answer`.
  - Hosting flow: `host_flow` = acquire VPS lock -> `_world_down` (asks before downloading a newer shared world) -> `_ensure_runtime` (auto-downloads jar and Java) -> `start_server` (auto-backup first) -> `start_tunnel`. `stop_flow` -> `finish_hosting` uploads the world then releases the lock; on upload failure it keeps both and the page offers Retry. Without `sync_enabled` the same buttons just start/stop locally.
  - World settings page is generated from `FIELDS` (key, label, help, kind, options, default, group); adding a server.properties option means adding a row there.
  - Players are tracked by regex-parsing server log lines in `_server_line`; OP/whitelist/bans come from the server's json files; actions are plain console commands.
  - Security: binds 127.0.0.1 only; middleware rejects non-localhost `Host` headers (DNS rebinding) and API calls without the per-run token (other websites cannot read it).
- `web/index.html`: the whole UI (vanilla JS, inline CSS, no external assets so it works offline and inside the exe).
- `core.py`: config load/save (`DEFAULT_CONFIG`, layered under `BASE/defaults.json`), in-place `server.properties` editing, EULA, Java detection and portable Temurin install, Mojang downloads (SHA1-checked), SSH key helpers, `ManagedProcess` (asyncio subprocess streaming lines to a callback).
- `world.py`: backups (`backup_world`/`restore_world`; zips include server.properties/ops/whitelist under `_server/`) and the VPS world store, reached only through `sftp -b` batches (`_run_sftp`). The lock is the directory `lock/` created with sftp `mkdir` (atomic). Never use sftp `rename` for locking; real servers overwrite. It holds `lock.json` + `heartbeat.json` (60 s; stale after 180 s). Worlds are `world-N.zip` + `meta.json`; local `sync_state.json` tracks the version and a `dirty` flag (hosted but not uploaded).
- Paths: `core.DATA_DIR` is `BASE` if `BASE/server` or `BASE/manager_config.json` exists (legacy installs), else `~/MinecraftServerManager` (an existing `%APPDATA%\MinecraftServerManager` from older builds is still used; `MCMGR_DATA` overrides). Avoid AppData for new paths: Microsoft Store Python silently redirects writes there. Frozen `BASE` is the exe's folder; `web/` comes from `sys._MEIPASS`. `core.running_from_temp()` refuses to run from inside a zip.

## Gotchas

- Anything that runs the app or calls `core.save_config` writes `manager_config.json`: always point `MCMGR_DATA`/`MCMGR_CONFIG` at temp paths when testing; never delete the real file.
- Validate config on a copy before applying (`Manager.save_config`): a bad value in the live config gets persisted by the next unrelated save.
- `online-mode=false` is intentional (TLauncher/cracked players); `enforce-secure-profile=false` is required for them to chat.
- 26.x needs Java 25+; `java_required` comes from Mojang's metadata at download time.
- VPS (`vps_setup.sh`): `mctunnel` may only forward port 25565, `mcsync` may only use sftp in `/srv/mcworld`; their `authorized_keys` are root-owned. `GatewayPorts clientspecified` is required for the public bind. Don't touch 3x-ui/Xray on that VPS.
- Owner's network: their direct connection is throttled (~6 KB/s downstream, data bundle) while their v2rayN proxy (SOCKS5 127.0.0.1:10808) is fast. Java/ssh ignore the Windows proxy, so the `proxy` setting routes ssh/sftp via `ProxyCommand=<app> --socks-pipe %h %p <proxy>` (`core.ssh_proxy_args`, handled at the top of `__main__`) and `Manager.update_join_proxy` serves `localhost:25566` -> VPS:25565 through the proxy for playing from that PC. Blank proxy = direct (friends).
- Always pass keys to ssh/sftp through `core.usable_key()`: Windows OpenSSH rejects keys other users can read, and exFAT/FAT drives (like the owner's H:) cannot store permissions, so it uses a hash-named copy in `~/MinecraftServerManager/.keys` locked with icacls.
- Backup retention lives in `world.prune_backups` (runs after every backup and at startup): keep `backup_keep` automatic start backups, 1 upload copy, 3 pre-restore copies, then delete the oldest automatic ones while over `backup_max_gb`. Manual backups and the newest backup are never auto-deleted. `backup_world` skips writing when the newest zip's comment (a fingerprint of the world files) matches.
- One manager per data folder: `DATA_DIR/manager.port` holds the running port; a second launch opens that URL and exits.
- Process model on Windows: `python app.py` (no args) is only the launcher window. It spawns `app.py --backend` with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` (own hidden console, so Java and ssh have no windows and survive the launcher), tails `DATA_DIR/manager.log`, and on console close/Ctrl+C/logoff POSTs `/api/shutdown` with the token it passed via `MCMGR_TOKEN`. The backend then runs `Manager.stop_flow` (stop, local backup, upload, release lock). `MCMGR_NO_LAUNCHER=1` runs the backend in the foreground (tests).
- `join.py` / `join.bat`: standalone join helper (stdlib + core only, no aiohttp, hosts nothing). Forwards localhost:25566 to the VPS through the configured or Windows-detected proxy, for playing on someone else's server from a throttled connection.
- "Online" means reachable: `Manager.probe_loop` sends a real Minecraft status ping (`world.mc_ping`) to the public VPS address every ~2 s (through the proxy if set) and exposes it as `ready`. The log's `Done!` only proves the server started locally; the page shows 'Friends can join now' only when `ready`, and other people see 'starting' (not 'online') until it is. `status_loop` polls the lock every 5 s while a host exists, 15 s when idle.
- Backups contain the world (including `world/players/{data,stats,advancements}` in 26.x) plus `_server/` copies of server.properties, ops, whitelist, bans, usercache.
