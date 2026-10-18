"""Minecraft Server Manager - a local web app.  Run:  python app.py  (or start.bat)

Serves a page on http://127.0.0.1:<port> (this PC only) and opens the browser.
All server/world/VPS logic lives in core.py and world.py; this file is the HTTP API
plus the "hosting" flow (lock -> world -> server -> tunnel) that the page drives.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
import webbrowser
from pathlib import Path
from collections import deque
from pathlib import Path

from aiohttp import web

import core
import world

# Bundled with PyInstaller: data files are unpacked to sys._MEIPASS.
WEB_DIR = Path(getattr(sys, "_MEIPASS", core.BASE)) / "web"
PORTS = range(8765, 8776)
NAME_RE = re.compile(r"\w{1,16}")

# server.properties shown on the World settings page.
# (key, label, help, kind, options, default, group)   kind: text | int | choice | toggle
FIELDS = [
    ("gamemode", "Game mode", "What new players start in.", "choice",
     ["survival", "creative", "adventure", "spectator"], "survival", "Gameplay"),
    ("difficulty", "Difficulty", "How dangerous mobs are.", "choice",
     ["peaceful", "easy", "normal", "hard"], "easy", "Gameplay"),
    ("pvp", "Player vs player", "Players can hurt each other.", "toggle", None, "true", "Gameplay"),
    ("hardcore", "Hardcore", "One life only; difficulty is forced to hard.", "toggle", None, "false", "Gameplay"),
    ("allow-flight", "Allow flying", "Stops the server kicking players who fly (needed for some mods).",
     "toggle", None, "false", "Gameplay"),
    ("motd", "Server name", "Shown in the multiplayer server list.", "text", None, "A Minecraft Server", "Server"),
    ("max-players", "Max players", "How many people can be online at once.", "int", None, "20", "Server"),
    ("white-list", "Whitelist only", "Only players you add on the Players page can join.",
     "toggle", None, "false", "Server"),
    ("online-mode", "Official accounts only", "Turn OFF so TLauncher / cracked players can join.",
     "toggle", None, "true", "Server"),
    ("enforce-secure-profile", "Require signed chat", "Keep OFF so cracked players can chat.",
     "toggle", None, "false", "Server"),
    ("view-distance", "View distance", "Chunks sent to players (lower = less lag).", "int", None, "10", "World"),
    ("level-seed", "World seed", "Only used when a new world is created. Blank = random.",
     "text", None, "", "World"),
    ("level-name", "World folder", "Folder name of the world inside the server folder.",
     "text", None, "world", "World"),
    ("enable-command-block", "Command blocks", "Allow command blocks to run.", "toggle", None, "false", "Advanced"),
    ("server-port", "Server port", "Port Minecraft listens on (the tunnel forwards it).",
     "int", None, "25565", "Advanced"),
]
CONFIG_KEYS = ["player_name", "version", "ram_min", "ram_max", "java", "sync_enabled", "tunnel_autostart",
               "ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_remote_port", "ssh_bind",
               "sync_user", "sync_key", "backup_keep", "backup_max_gb", "proxy"]
JOIN_PORT = 25566       # "join through my proxy" helper listens here


class Log:
    """Ring buffer of lines with a running sequence number, so the page can fetch only new lines."""

    def __init__(self, size: int):
        self.lines: deque[tuple[int, str]] = deque(maxlen=size)
        self.seq = 0

    def add(self, line: str) -> None:
        self.seq += 1
        self.lines.append((self.seq, line))

    def since(self, n: int) -> list[str]:
        return [line for s, line in self.lines if s > n]


class Manager:
    def __init__(self) -> None:
        self.cfg = core.load_config()
        self.server = core.ManagedProcess(self._server_line, self._server_exit)
        self.tunnel = core.ManagedProcess(self._tunnel_line, self._tunnel_exit)
        self.log, self.tlog, self.notes = Log(5000), Log(1000), Log(50)
        self.server_state = "stopped"          # stopped | starting | online | stopping
        self.tunnel_state = "off"              # off | connecting | up | retrying
        self.online: set[str] = set()
        self.hosting = False
        self.lock_id = ""
        self.pending_upload = False
        self.busy = ""                         # name of the running long operation, if any
        self.step = ""                         # what it is doing right now
        self.progress: float | None = None     # 0-100 for downloads
        self.question: dict | None = None      # a choice the page must show as a dialog
        self._answer: asyncio.Future | None = None
        self.host_status: dict | None = None
        self._tunnel_wanted = False
        self.stopping = asyncio.Event()
        self.ran_server = False                # started this session -> back up when stopping
        self.join_server: asyncio.base_events.Server | None = None
        self.ready = False                     # a real Minecraft status ping to the public address works
        self.public: dict | None = None
        self.apply_retention()

    def apply_retention(self) -> list[str]:
        world.set_retention(int(self.cfg["backup_keep"]), float(self.cfg["backup_max_gb"]))
        return world.prune_backups()

    # ------------------------------------------------------------ helpers
    def note(self, text: str, level: str = "info") -> None:
        self.notes.add(f"{level}|{text}")

    async def ask(self, text: str, options: list[str]) -> str:
        """Show a dialog in the page and wait for the user to pick one of `options`."""
        self._answer = asyncio.get_running_loop().create_future()
        self.question = {"id": uuid.uuid4().hex[:8], "text": text, "options": options}
        try:
            return await self._answer
        finally:
            self.question, self._answer = None, None

    def run_task(self, name: str, coro) -> bool:
        """Run one long operation at a time in the background; errors become notifications."""
        if self.busy:
            coro.close()
            self.note(f"Please wait - still busy: {self.busy}", "warning")
            return False
        self.busy = name

        async def wrapper():
            try:
                await coro
            except world.SyncError as e:
                self.note(str(e), "error")
            except Exception as e:
                self.note(f"{name} failed: {e}", "error")
            finally:
                self.busy, self.step, self.progress = "", "", None

        asyncio.get_running_loop().create_task(wrapper())
        return True

    def _prog(self, p: float) -> None:      # called from download threads
        self.progress = p

    def _step_cb(self, msg: str) -> None:   # called from sync threads
        self.step = msg

    def props(self) -> dict[str, str]:
        return core.read_properties(core.SERVER_DIR / "server.properties")

    def sync_on(self) -> bool:
        c = self.cfg
        return bool(c["sync_enabled"] and c["ssh_host"] and c["sync_user"])

    # ------------------------------------------------------------ server process
    async def start_server(self) -> None:
        if self.server.running:
            return
        c = self.cfg
        if not (core.SERVER_DIR / "server.jar").exists():
            raise RuntimeError("Minecraft is not downloaded yet (Setup page).")
        if not core.eula_accepted():
            raise RuntimeError("Accept the Minecraft EULA first (Home page).")
        if not all(re.fullmatch(r"\d+[MG]", c[k]) for k in ("ram_min", "ram_max")):
            raise RuntimeError("The memory setting is invalid. Fix it on the Setup page.")
        if not (core.SERVER_DIR / "server.properties").exists():
            self.save_settings({})
        self.step = "Making a safety backup..."
        await asyncio.to_thread(world.backup_world, "auto")
        self.online.clear()
        self.server_state = "starting"
        self.log.add("--- starting server ---")
        self.ran_server = True
        await self.server.start([c["java"], f"-Xms{c['ram_min']}", f"-Xmx{c['ram_max']}",
                                 "-jar", "server.jar", "nogui"], cwd=core.SERVER_DIR)

    async def stop_server(self) -> None:
        if self.server.running:
            self.server_state = "stopping"
            await self.server.stop("stop", timeout=90)

    def _server_line(self, line: str) -> None:
        self.log.add(line)
        if m := re.search(r"\]: (\S+)\[/.*\] logged in with entity id", line):
            self.online.add(m.group(1))
        elif m := re.search(r"\]: (\S+) lost connection", line):
            self.online.discard(m.group(1))
        if self.server_state == "starting" and re.search(r"Done \(.*\)! For help", line):
            self.server_state = "online"

    def _server_exit(self, code: int | None) -> None:
        crashed = self.server_state != "stopping"
        self.online.clear()
        self.server_state = "stopped"
        self.log.add(f"--- server stopped (exit code {code}) ---")
        if crashed:
            self.note("The server stopped unexpectedly. Open the Console page to see why.", "error")
            if not self.sync_on():      # nothing to upload: go back to a clean "offline" state
                self.hosting = False
                asyncio.ensure_future(self.stop_tunnel())
    # ------------------------------------------------------------ tunnel
    async def start_tunnel(self) -> None:
        if self.tunnel.running:
            return
        c = self.cfg
        exe = core.find_tool("ssh")
        if not exe or not c["ssh_host"] or not c["ssh_user"]:
            self.note("Tunnel not started: VPS connection is not set up (Setup page > Connection).", "warning")
            return
        port = self.props().get("server-port", "25565")
        args = [exe, "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "StrictHostKeyChecking=accept-new", "-p", c["ssh_port"] or "22"]
        if c["ssh_key"]:
            args += ["-i", core.usable_key(c["ssh_key"])]
        args += core.ssh_proxy_args(c)
        args += ["-R", f"{c['ssh_bind'] or '0.0.0.0'}:{c['ssh_remote_port']}:localhost:{port}",
                 f"{c['ssh_user']}@{c['ssh_host']}"]
        self._tunnel_wanted = True
        self.tunnel_state = "connecting"
        await self.tunnel.start(args)
        asyncio.get_running_loop().call_later(6, self._tunnel_check)

    def _tunnel_check(self) -> None:
        # ssh -N prints nothing on success; still running after a few seconds = forward is up
        if self.tunnel.running:
            self.tunnel_state = "up"

    async def stop_tunnel(self) -> None:
        self._tunnel_wanted = False
        await self.tunnel.stop()
        self.tunnel_state = "off"

    def _tunnel_line(self, line: str) -> None:
        self.tlog.add(line)

    def _tunnel_exit(self, code: int | None) -> None:
        self.tlog.add(f"--- tunnel exited (code {code}) ---")
        if self._tunnel_wanted:
            self.tunnel_state = "retrying"
            asyncio.get_running_loop().call_later(5, lambda: asyncio.ensure_future(self._tunnel_retry()))
        else:
            self.tunnel_state = "off"

    async def _tunnel_retry(self) -> None:
        if self._tunnel_wanted and not self.tunnel.running:
            await self.start_tunnel()

    # ------------------------------------------------------------ hosting flow
    async def host_flow(self) -> None:
        if self.hosting or self.server.running:
            return
        c = self.cfg
        if self.sync_on():
            name = c["player_name"].strip()
            if not name:
                raise RuntimeError("Type your name on the Home page first.")
            self.step = "Checking nobody else is hosting..."
            self.lock_id = uuid.uuid4().hex
            ok, holder = await asyncio.to_thread(world.acquire_lock, c, name, self.lock_id)
            if not ok:
                self.note(f"Already running - hosted by {(holder or {}).get('holder', '?')}. "
                          "Just join the server instead.", "error")
                self.lock_id = ""
                return
            self.hosting = True
            try:
                if not await self._world_down():
                    await self._abort()
                    return
            except Exception:
                await self._abort()
                raise
        else:
            self.hosting = True
        try:
            await self._ensure_runtime()
            if self.sync_on():
                world.mark_dirty()
            self.step = "Starting the server..."
            await self.start_server()
            await self.start_tunnel()
        except Exception:
            if not self.server.running:
                await self._abort()
            raise
        self.note("Server is starting. It is online when the status turns green.")

    async def _abort(self) -> None:
        """Give the lock back when hosting failed before the server ran (nothing to upload)."""
        self.hosting = False
        if self.sync_on() and self.lock_id:
            try:
                await asyncio.to_thread(world.release_lock, self.cfg, self.lock_id)
            except world.SyncError:
                pass
        self.lock_id = ""

    async def _world_down(self) -> bool:
        c = self.cfg
        self.step = "Checking the shared world..."
        info = await asyncio.to_thread(world.check_remote, c)
        if info["dirty"]:
            if info["remote_version"] > info["local_version"]:
                self.note("Your last session was never uploaded, and a newer shared world exists. "
                          "Make a backup on the Backups page, then restore the one you want to keep.", "error")
                return False
            await asyncio.to_thread(world.push_world, c, c["player_name"], self._step_cb)
            return True
        if info["newer"]:
            mine = ("This PC has no world yet." if not info["has_local_world"]
                    else "Your copy is older. It will be backed up first, so nothing is lost.")
            choice = await self.ask(
                f"A newer shared world is available: version {info['remote_version']}, saved by "
                f"{info['by']} on {_when(info['time'])}.\n\n{mine}",
                ["Download it and start", "Cancel"])
            if choice != "Download it and start":
                return False
            await asyncio.to_thread(world.pull_world, c, self._step_cb)
        return True

    async def _ensure_runtime(self) -> None:
        """Download whatever is missing: server.jar, Java. EULA must already be accepted."""
        c = self.cfg
        if not core.eula_accepted():
            raise RuntimeError("Accept the Minecraft EULA on the Home page first.")
        if not (core.SERVER_DIR / "server.jar").exists():
            await self.download_minecraft(c["version"] or "")
        need = c.get("java_required") or 0
        java = await asyncio.to_thread(core.find_java, c["java"], need)
        if not java:
            self.step = f"Downloading Java {need or 21} (one time, about 50 MB)..."
            java = await asyncio.to_thread(core.install_java, need or 21, self._prog)
            self.progress = None
        if java != c["java"]:
            c["java"] = java
            core.save_config(c)

    async def download_minecraft(self, version: str) -> None:
        c = self.cfg
        if not version:
            version, _ = await asyncio.to_thread(core.list_release_versions, 1)
        self.step = f"Downloading Minecraft {version}..."
        c["java_required"] = await asyncio.to_thread(core.download_server, version, self._prog)
        c["version"] = version
        core.save_config(c)
        self.progress = None

    async def stop_flow(self) -> None:
        """Stop tunnel and server, back up locally, upload to the shared world, release the lock."""
        await self.stop_tunnel()
        if self.server.running:
            self.say("Stopping the server and saving the world...")
            await self.stop_server()
        if self.ran_server and world.has_world():
            self.say("Making a local backup...")
            out = await asyncio.to_thread(world.backup_world, "auto")
            if out:
                self.say(f"Local backup: {out.name}")
        if self.hosting and self.sync_on():
            self.say("Uploading the world to the shared storage...")
        await self.finish_hosting()
        self.ran_server = False

    def say(self, msg: str) -> None:
        """Progress shown in the page and printed to the manager window/log."""
        self.step = msg
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    async def finish_hosting(self) -> None:
        """Upload the world, then give the lock back. If the upload fails keep both; nothing is lost."""
        if not self.hosting:
            return
        if not self.sync_on():
            self.hosting = False
            return
        c = self.cfg
        try:
            await asyncio.to_thread(world.push_world, c, c["player_name"], self._step_cb)
            await asyncio.to_thread(world.release_lock, c, self.lock_id)
        except world.SyncError as e:
            self.pending_upload = True
            self.note(f"{e} Your world is safe on this PC - press 'Retry upload'.", "error")
            return
        self.pending_upload = False
        self.hosting = False
        self.lock_id = ""
        self.note("World saved to the shared storage. You are no longer hosting.", "success")
        print(f"[{time.strftime('%H:%M:%S')}] World uploaded to the shared storage.", flush=True)

    async def takeover(self) -> None:
        await asyncio.to_thread(world.release_lock, self.cfg, "", True)
        await self.host_flow()

    # ------------------------------------------------------------ join through proxy
    async def update_join_proxy(self) -> None:
        """Listen on localhost:JOIN_PORT and forward Minecraft to the VPS through the proxy.

        Minecraft ignores Windows proxy settings, so this lets a player on this PC use the proxy."""
        want = bool(core.parse_proxy(self.cfg["proxy"]) and self.cfg["ssh_host"])
        if self.join_server and not want:
            self.join_server.close()
            self.join_server = None
        elif want and not self.join_server:
            try:
                self.join_server = await asyncio.start_server(self._join_client, "127.0.0.1", JOIN_PORT)
            except OSError:
                self.note(f"Port {JOIN_PORT} is busy, so 'join through proxy' is unavailable.", "warning")

    async def _join_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        c = self.cfg
        try:
            r2, w2 = await core.socks_open(c["ssh_host"], int(c["ssh_remote_port"] or 25565),
                                           core.parse_proxy(c["proxy"]))
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            self.note(f"Could not reach the server through the proxy: {e}", "error")
            writer.close()
            return

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while (data := await src.read(65536)):
                    dst.write(data)
                    await dst.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pump(reader, w2), pump(r2, writer))

    # ------------------------------------------------------------ background loops
    async def probe_loop(self) -> None:
        """Every few seconds ask the public address for a Minecraft status. Cheap, and it is the only
        thing that proves friends can really join (the log's 'Done!' only proves it started here)."""
        while True:
            c = self.cfg
            result = None
            if c["ssh_host"]:
                try:
                    result = await asyncio.to_thread(world.mc_ping, c["ssh_host"], int(c["ssh_remote_port"] or 25565),
                                                     core.parse_proxy(c["proxy"]))
                except Exception:
                    result = None
            self.ready, self.public = result is not None, result
            await asyncio.sleep(2)

    async def status_loop(self) -> None:
        beat = 0.0
        while True:
            try:
                if self.sync_on():
                    self.host_status = await asyncio.to_thread(world.host_status, self.cfg)
                else:
                    self.host_status = None
                if self.hosting and self.sync_on() and time.time() - beat > 60:
                    beat = time.time()
                    await asyncio.to_thread(world.heartbeat, self.cfg, self.cfg["player_name"], self.lock_id)
            except Exception as e:
                self.host_status = {"state": "error", "msg": str(e)}
            busy = self.hosting or (self.host_status or {}).get("state") not in (None, "free")
            await asyncio.sleep(5 if busy else 15)     # follow a start/stop quickly, idle slowly

    async def shutdown(self) -> None:
        await self.stop_flow()
        if self.pending_upload:
            self.say("Upload failed - the world is safe on this PC and will be uploaded next time you start.")

    # ------------------------------------------------------------ settings / config
    def settings_view(self) -> list[dict]:
        p = self.props()
        return [{"key": k, "label": label, "help": help_, "kind": kind, "options": opts,
                 "value": p.get(k, default), "group": group}
                for k, label, help_, kind, opts, default, group in FIELDS]

    def save_settings(self, values: dict) -> None:
        updates = {}
        for k, _l, _h, kind, opts, default, _g in FIELDS:
            v = str(values.get(k, self.props().get(k, default))).strip()
            if kind == "toggle":
                v = "true" if v.lower() in ("true", "1", "on") else "false"
            elif kind == "int":
                v = v if v.isdigit() else default
            elif kind == "choice" and v not in opts:
                v = default
            elif kind == "text" and k != "level-seed" and not v:
                v = default
            updates[k] = v
        if updates["hardcore"] == "true":
            updates["difficulty"] = "hard"   # the game forces this anyway
        core.write_properties(core.SERVER_DIR / "server.properties", updates)

    def config_view(self) -> dict:
        c = self.cfg
        need = c.get("java_required") or 0
        major = core.java_major(c["java"])
        return {**{k: c[k] for k in CONFIG_KEYS}, "java_required": need, "java_major": major,
                "windows_proxy": core.windows_proxy(),
                "java_ok": major is not None and major >= need, "eula": core.eula_accepted(),
                "jar": (core.SERVER_DIR / "server.jar").exists(), "data_dir": str(core.DATA_DIR)}

    def save_config(self, values: dict) -> None:
        new = dict(self.cfg)            # validate a copy, so a bad value never reaches the live config
        for k in CONFIG_KEYS:
            if k in values:
                v = values[k]
                new[k] = bool(v) if isinstance(core.DEFAULT_CONFIG.get(k), bool) else str(v).strip()
        for k in ("ram_min", "ram_max"):
            new[k] = new[k].upper()
            if not re.fullmatch(r"\d+[MG]", new[k]):
                raise ValueError("Memory must look like 2G or 512M")
        new["java"] = new["java"] or "java"
        if new["proxy"] and not core.parse_proxy(new["proxy"]):
            raise ValueError("Proxy must look like 127.0.0.1:10808 (or leave it empty)")
        if not (new["backup_keep"].isdigit() and 1 <= int(new["backup_keep"]) <= 100):
            raise ValueError("Number of backups to keep must be between 1 and 100")
        try:
            if not 0.5 <= float(new["backup_max_gb"]) <= 10000:
                raise ValueError
        except ValueError:
            raise ValueError("Backup space limit must be a number of GB (at least 0.5)")
        self.cfg.update(new)
        core.save_config(self.cfg)


def _when(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "?"


def _names(fn: str) -> set[str]:
    import json
    try:
        return {e["name"] for e in json.loads((core.SERVER_DIR / fn).read_text(encoding="utf-8")) if "name" in e}
    except (OSError, ValueError):
        return set()


def _backups() -> list[dict]:
    if not core.BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(core.BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        kind = p.name.split("-")[0]
        out.append({"name": p.name, "size": st.st_size, "time": st.st_mtime,
                    "kind": {"auto": "Automatic", "manual": "Made by you",
                             "sync": "Shared-world upload", "pre-import": "Before a restore"}.get(kind, kind)})
    return out


# ================================================================== HTTP API
def make_app(mgr: Manager, token: str) -> web.Application:
    routes = web.RouteTableDef()

    @web.middleware
    async def guard(request: web.Request, handler):
        # Only this PC, and only our own page: blocks DNS rebinding and other websites calling the API.
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden(text="Local access only")
        if request.path.startswith("/api/") and request.headers.get("X-Token") != token \
                and request.query.get("token") != token:
            raise web.HTTPForbidden(text="Bad token")
        return await handler(request)

    def ok(**kw):
        return web.json_response({"ok": True, **kw})

    def fail(msg: str, status: int = 400):
        return web.json_response({"ok": False, "error": msg}, status=status)

    async def body(request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    @routes.get("/")
    async def index(request):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    @routes.get("/api/state")
    async def state(request):
        q = request.query
        log_n, tlog_n, note_n = (int(q.get(k, 0) or 0) for k in ("log", "tlog", "note"))
        c = mgr.cfg
        return web.json_response({
            "server": mgr.server_state, "tunnel": mgr.tunnel_state, "hosting": mgr.hosting,
            "pending_upload": mgr.pending_upload, "busy": mgr.busy, "step": mgr.step,
            "progress": mgr.progress, "question": mgr.question, "host_status": mgr.host_status,
            "sync": mgr.sync_on(), "online": sorted(mgr.online), "name": c["player_name"],
            "ready": mgr.ready, "app_version": core.APP_VERSION,
            "eula": core.eula_accepted(), "version": c["version"],
            "address": f"{c['ssh_host']}:{c['ssh_remote_port']}" if c["ssh_host"] else "",
            "join_proxy": f"localhost:{JOIN_PORT}" if mgr.join_server else "",
            "log": mgr.log.since(log_n), "log_seq": mgr.log.seq,
            "tlog": mgr.tlog.since(tlog_n), "tlog_seq": mgr.tlog.seq,
            "notes": mgr.notes.since(note_n), "note_seq": mgr.notes.seq,
        })

    @routes.post("/api/answer")
    async def answer(request):
        b = await body(request)
        if mgr.question and b.get("id") == mgr.question["id"] and mgr._answer and not mgr._answer.done():
            mgr._answer.set_result(b.get("value"))
        return ok()

    @routes.post("/api/host/start")
    async def host_start(request):
        mgr.run_task("Starting", mgr.host_flow())
        return ok()

    @routes.post("/api/host/stop")
    async def host_stop(request):
        mgr.run_task("Stopping", mgr.stop_flow())
        return ok()

    @routes.post("/api/host/retry")
    async def host_retry(request):
        mgr.run_task("Uploading", mgr.finish_hosting())
        return ok()

    @routes.post("/api/host/takeover")
    async def host_takeover(request):
        mgr.run_task("Taking over", mgr.takeover())
        return ok()

    @routes.post("/api/server/restart")
    async def restart(request):
        if not mgr.server.running:
            return fail("The server is not running")
        async def go():
            await mgr.stop_server()
            await mgr.start_server()
        mgr.run_task("Restarting", go())
        return ok()

    @routes.post("/api/command")
    async def command(request):
        b = await body(request)
        cmd = str(b.get("cmd", "")).strip().lstrip("/")
        if b.get("chat"):
            cmd = f"say {str(b.get('cmd', '')).strip()}"
        if not cmd:
            return fail("Empty command")
        if not mgr.server.running:
            return fail("The server is not running")
        mgr.log.add(f"> {cmd}")
        await mgr.server.send(cmd)
        return ok()

    @routes.get("/api/players")
    async def players(request):
        ops, wl = _names("ops.json"), _names("whitelist.json")
        banned = _names("banned-players.json")
        everyone = sorted(mgr.online | ops | wl | banned, key=str.lower)
        return web.json_response({"players": [
            {"name": n, "online": n in mgr.online, "op": n in ops, "whitelisted": n in wl, "banned": n in banned}
            for n in everyone]})

    @routes.post("/api/players/action")
    async def player_action(request):
        b = await body(request)
        name, action = str(b.get("name", "")).strip(), b.get("action")
        if not NAME_RE.fullmatch(name):
            return fail("Player names are 1-16 letters, numbers or _")
        cmds = {"op": "op {n}", "deop": "deop {n}", "kick": "kick {n}", "ban": "ban {n}",
                "pardon": "pardon {n}", "wl_add": "whitelist add {n}", "wl_remove": "whitelist remove {n}",
                "gamemode": "gamemode {m} {n}", "heal": "effect give {n} minecraft:instant_health 1 10"}
        if action not in cmds:
            return fail("Unknown action")
        mode = b.get("mode", "survival")
        if action == "gamemode" and mode not in ("survival", "creative", "adventure", "spectator"):
            return fail("Unknown game mode")
        if not mgr.server.running:
            return fail("Start the server first")
        cmd = cmds[action].format(n=name, m=mode)
        mgr.log.add(f"> {cmd}")
        await mgr.server.send(cmd)
        return ok()

    @routes.get("/api/settings")
    async def get_settings(request):
        return web.json_response({"fields": mgr.settings_view(), "running": mgr.server.running})

    @routes.post("/api/settings")
    async def set_settings(request):
        mgr.save_settings(await body(request))
        return ok(restart_needed=mgr.server.running)

    @routes.get("/api/config")
    async def get_config(request):
        return web.json_response(await asyncio.to_thread(mgr.config_view))

    @routes.post("/api/config")
    async def set_config(request):
        b = await body(request)
        try:
            mgr.save_config(b)
        except ValueError as e:
            return fail(str(e))
        await mgr.update_join_proxy()
        if "eula" in b:
            core.set_eula(bool(b["eula"]))
        return ok()

    # ---- friends / invites (talks to mc-hub on the VPS)
    def hub_proxy():
        return core.parse_proxy(mgr.cfg["proxy"])

    async def hub_admin(method: str, path: str, data: dict | None = None) -> dict:
        h = core.load_hub()
        if not h:
            raise RuntimeError("This PC is not connected as the owner yet.")
        if not h.get("fp"):
            raise RuntimeError("Reconnect as the owner: paste the owner code from 'mc-hub owner-code' again.")
        return await asyncio.to_thread(core.hub_call, h["host"], h["port"], method, path, data,
                                       h["token"], hub_proxy(), 15, h["fp"])

    @routes.get("/api/hub")
    async def hub_state(request):
        h = core.load_hub()
        out = {"owner": bool(h), "host": h.get("host", ""), "friends": [], "legacy": 0, "error": "",
               "hub_version": ""}
        if h:
            try:
                r = await hub_admin("GET", "/admin/friends")
                out["friends"], out["legacy"] = r["friends"], r["legacy"]
                out["hub_version"] = r.get("version", "")
            except RuntimeError as e:
                out["error"] = str(e)
        return web.json_response(out)

    @routes.post("/api/hub/owner")
    async def hub_owner(request):
        b = await body(request)
        try:
            host, port, token, fp = core.split_hub(str(b.get("code", "")))
            await asyncio.to_thread(core.hub_call, host, port, "GET", "/admin/friends", None, token,
                                    hub_proxy(), 15, fp)
        except (ValueError, RuntimeError) as e:
            return fail(str(e))
        core.save_hub({"host": host, "port": port, "token": token, "fp": fp})
        if not mgr.cfg["ssh_host"]:
            mgr.save_config({"ssh_host": host})
        return ok()

    @routes.post("/api/hub/invite")
    async def hub_invite(request):
        try:
            r = await hub_admin("POST", "/admin/invite", {})
        except RuntimeError as e:
            return fail(str(e))
        h = core.load_hub()
        return ok(code=r["code"], host=h["host"], text=f"{h['host']}:{h['port']}/{r['code']}/{h['fp'][:20]}")

    @routes.post("/api/hub/remove")
    async def hub_remove(request):
        b = await body(request)
        try:
            await hub_admin("POST", "/admin/remove-legacy" if b.get("legacy") else "/admin/remove",
                            {"name": str(b.get("name", ""))})
        except RuntimeError as e:
            return fail(str(e))
        return ok()

    @routes.post("/api/hub/join")
    async def hub_join(request):
        """Friend side: make this PC's own key, send only its public half with the invite code."""
        b = await body(request)
        name = str(b.get("name", "")).strip()
        try:
            host, port, code, fp = core.split_hub(str(b.get("invite", "")))
            key = await asyncio.to_thread(core.own_key)
            pub = Path(key + ".pub").read_text().strip()
            r = await asyncio.to_thread(core.hub_call, host, port, "POST", "/enroll",
                                        {"code": code, "name": name, "pubkey": pub}, "", hub_proxy(), 15, fp)
            mgr.save_config({"ssh_host": host, "ssh_user": r["ssh_user"], "ssh_port": str(r["ssh_port"]),
                             "ssh_remote_port": str(r["remote_port"]), "ssh_key": key, "sync_user": r["sync_user"],
                             "sync_key": key, "sync_enabled": True, "player_name": name})
        except (ValueError, RuntimeError) as e:
            return fail(str(e))
        return ok()

    @routes.get("/api/versions")
    async def versions(request):
        try:
            latest, vs = await asyncio.to_thread(core.list_release_versions)
        except Exception as e:
            return fail(f"Could not reach Mojang: {e}")
        return web.json_response({"latest": latest, "versions": vs})

    @routes.post("/api/minecraft/download")
    async def mc_download(request):
        if mgr.server.running:
            return fail("Stop the server before changing the Minecraft version")
        v = str((await body(request)).get("version", ""))
        mgr.run_task("Downloading Minecraft", mgr.download_minecraft(v))
        return ok()

    @routes.post("/api/java/install")
    async def java_install(request):
        async def go():
            need = mgr.cfg.get("java_required") or 21
            mgr.step = f"Downloading Java {need}..."
            path = await asyncio.to_thread(core.install_java, need, mgr._prog)
            mgr.cfg["java"] = path
            core.save_config(mgr.cfg)
            mgr.note("Java installed", "success")
        mgr.run_task("Installing Java", go())
        return ok()

    @routes.get("/api/backups")
    async def backups(request):
        remote = None
        if mgr.sync_on():
            try:
                remote = await asyncio.to_thread(world.check_remote, mgr.cfg)
            except Exception as e:
                remote = {"error": str(e)}
        items = _backups()
        return web.json_response({"backups": items, "remote": remote, "dir": str(core.BACKUP_DIR),
                                  "total": sum(b["size"] for b in items), "keep": mgr.cfg["backup_keep"],
                                  "max_gb": mgr.cfg["backup_max_gb"],
                                  "has_world": world.has_world(), "running": mgr.server.running})

    @routes.post("/api/backups/retention")
    async def backup_retention(request):
        b = await body(request)
        try:
            mgr.save_config({k: b[k] for k in ("backup_keep", "backup_max_gb") if k in b})
        except ValueError as e:
            return fail(str(e))
        deleted = await asyncio.to_thread(mgr.apply_retention)
        return ok(deleted=len(deleted))

    @routes.post("/api/backups/delete")
    async def backup_delete(request):
        name = str((await body(request)).get("name", ""))
        if not await asyncio.to_thread(world.delete_backup, name):
            return fail("Backup not found")
        return ok()

    @routes.post("/api/backups/create")
    async def backup_create(request):
        if mgr.server.running:
            await mgr.server.send("save-all flush")
            await asyncio.sleep(3)

        async def go():
            mgr.step = "Making a backup..."
            started = time.time()
            out = await asyncio.to_thread(world.backup_world, "manual")
            if not out:
                mgr.note("There is no world to back up yet", "warning")
            elif out.stat().st_mtime < started - 1:
                mgr.note(f"Nothing changed since the last backup ({out.name}), so no new copy was needed.")
            else:
                mgr.note(f"Backup saved: {out.name}", "success")
        mgr.run_task("Backing up", go())
        return ok()

    def _backup_path(name: str) -> Path | None:
        p = (core.BACKUP_DIR / name).resolve()
        return p if p.parent == core.BACKUP_DIR.resolve() and p.suffix == ".zip" and p.exists() else None

    async def _restore(path: Path, cleanup: bool = False):
        try:
            mgr.step = "Restoring the world..."
            await asyncio.to_thread(world.restore_world, path)
            mgr.note("World restored. Your previous world was backed up first.", "success")
            if mgr.sync_on():
                choice = await mgr.ask("Make this the shared world for everyone? It replaces the shared "
                                       "world the next time you stop hosting.",
                                       ["Yes, share it", "No, keep it on this PC"])
                if choice == "Yes, share it":
                    await asyncio.to_thread(world.adopt_local, mgr.cfg)
                    mgr.note("Done. Start hosting, then Stop hosting to upload it.", "success")
        finally:
            if cleanup:
                path.unlink(missing_ok=True)

    @routes.post("/api/backups/restore")
    async def backup_restore(request):
        if mgr.server.running or mgr.hosting:
            return fail("Stop the server first")
        p = _backup_path(str((await body(request)).get("name", "")))
        if not p:
            return fail("Backup not found")
        mgr.run_task("Restoring", _restore(p))
        return ok()

    @routes.post("/api/backups/upload")
    async def backup_upload(request):
        if mgr.server.running or mgr.hosting:
            return fail("Stop the server first")
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=core.DATA_DIR)
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 16):
                f.write(chunk)
        mgr.run_task("Restoring", _restore(Path(tmp), cleanup=True))
        return ok()

    @routes.get("/api/backups/file/{name}")
    async def backup_file(request):
        p = _backup_path(request.match_info["name"])
        if not p:
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

    @routes.post("/api/open-folder")
    async def open_folder(request):
        target = core.BACKUP_DIR if (await body(request)).get("which") == "backups" else core.DATA_DIR
        target.mkdir(parents=True, exist_ok=True)
        if core.IS_WIN:
            os.startfile(target)
        return ok(path=str(target))

    @routes.post("/api/shutdown")
    async def shutdown(request):
        mgr.stopping.set()
        return ok()

    app = web.Application(middlewares=[guard], client_max_size=8 * 1024 ** 3)
    app.add_routes(routes)
    return app


PORT_FILE = core.DATA_DIR / "manager.port"


def already_running() -> str | None:
    """URL of a manager already serving this data folder, if any (so a second launch just opens it)."""
    import urllib.request
    try:
        port = int(PORT_FILE.read_text().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            if b"Minecraft Server Manager" in r.read(4000):
                return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError):
        pass
    return None


async def main() -> None:
    url = await asyncio.to_thread(already_running)
    if url:
        print(f"The manager is already open at {url} - opening it in your browser.")
        if not os.environ.get("MCMGR_NO_BROWSER"):
            webbrowser.open(url)
        return
    mgr = Manager()
    token = os.environ.get("MCMGR_TOKEN") or secrets.token_urlsafe(24)
    app = make_app(mgr, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = None
    for p in PORTS:
        try:
            await web.TCPSite(runner, "127.0.0.1", p).start()
            port = p
            break
        except OSError:
            continue
    if port is None:
        print("No free port between 8765 and 8775 - is the manager already open?")
        return
    url = f"http://127.0.0.1:{port}/"
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port))
    print(f"Minecraft Server Manager is running at {url}", flush=True)
    status = asyncio.get_running_loop().create_task(mgr.status_loop())
    probe = asyncio.get_running_loop().create_task(mgr.probe_loop())
    await mgr.update_join_proxy()
    if not os.environ.get("MCMGR_NO_BROWSER"):
        webbrowser.open(url)
    try:
        await mgr.stopping.wait()
    except asyncio.CancelledError:      # Ctrl+C
        pass
    finally:
        print(f"[{time.strftime('%H:%M:%S')}] Shutting down...", flush=True)
        status.cancel()
        probe.cancel()
        await mgr.shutdown()
        await runner.cleanup()
        PORT_FILE.unlink(missing_ok=True)
        print(f"[{time.strftime('%H:%M:%S')}] Done. Safe to turn off.", flush=True)


LOG_FILE = core.DATA_DIR / "manager.log"
_handler_ref = None          # keep the ctypes callback alive


def launcher() -> int:
    """The black window. Closing it (X, Ctrl+C, logoff) asks the background manager to shut down
    safely; the background process is not attached to this window, so it can take as long as it needs
    to stop the server, back up and upload, even after this window is gone."""
    import ctypes
    import subprocess
    import urllib.request

    if core.IS_WIN:
        ctypes.windll.kernel32.SetConsoleTitleW("Minecraft Server Manager")
    print("Minecraft Server Manager")
    print("=" * 60)
    url = already_running()
    if url:
        print(f"Already running at {url} - opening it in your browser.")
        if not os.environ.get("MCMGR_NO_BROWSER"):
            webbrowser.open(url)
        time.sleep(3)
        return 0

    token = secrets.token_urlsafe(24)
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "w", encoding="utf-8")
    cmd = ([sys.executable, "--backend"] if getattr(sys, "frozen", False)
           else [sys.executable, str(Path(__file__).resolve()), "--backend"])
    env = {**os.environ, "MCMGR_TOKEN": token, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    # Own hidden console + own process group: closing this window does not reach it (or Java under it).
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, cwd=str(core.BASE))
    except OSError:      # some terminals put us in a job that forbids breakaway
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, cwd=str(core.BASE))

    asked = {"done": False}

    def request_shutdown() -> None:
        if asked["done"]:
            return
        asked["done"] = True
        try:
            port = int(PORT_FILE.read_text().strip())
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                         headers={"X-Token": token, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).close()
        except (OSError, ValueError):
            pass

    if core.IS_WIN:
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def on_console_event(event: int) -> bool:
            # 0 Ctrl+C, 1 Ctrl+Break, 2 window closed, 5 logoff, 6 shutdown
            if event == 2 or event >= 5:
                request_shutdown()
                return True
            if event in (0, 1):
                print("\nStopping safely - please wait...", flush=True)
                request_shutdown()
                return True
            return False

        global _handler_ref
        _handler_ref = HandlerRoutine(on_console_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref, True)

    print("Starting...  Your browser opens in a moment.")
    print("Closing this window stops the server safely: it saves the world, makes a backup")
    print("and uploads it before turning off. That keeps running in the background after")
    print("the window closes, so give it a minute before shutting down the PC.")
    print("-" * 60, flush=True)
    pos = 0
    while True:
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                print(chunk, end="", flush=True)
        except OSError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    log.close()
    if proc.returncode:
        print(f"\nThe manager stopped with an error (code {proc.returncode}). Details: {LOG_FILE}")
        input("Press Enter to close...")
    return proc.returncode or 0


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--socks-pipe":     # used by ssh as ProxyCommand
        core.socks_pipe_main(*sys.argv[2:5])
        sys.exit(0)
    if core.running_from_temp():
        print("This program is running from a temporary folder (probably straight from the zip).")
        print("Your settings and world would be lost. Extract the zip to a normal folder first")
        print("(right-click the zip > Extract All), then start it from there.")
        input("Press Enter to close...")
        sys.exit(1)
    if "--backend" in sys.argv or not core.IS_WIN or os.environ.get("MCMGR_NO_LAUNCHER"):
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(launcher())
