"""Non-UI logic for the Minecraft Server Manager: downloads, properties, processes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Awaitable, Callable

APP_NAME = "MinecraftServerManager"
# Folder holding the code (or the .exe): defaults.json and keys/ ship here.
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _app_version() -> str:
    for d in (Path(getattr(sys, "_MEIPASS", BASE)), BASE, BASE.parent.parent):
        try:
            return (d / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "dev"


APP_VERSION = _app_version()


def running_from_temp() -> bool:
    """True when started from inside a zip viewer / temp folder (settings and world would be lost)."""
    s = str(BASE).lower()
    return "temp1_" in s or str(Path(os.environ.get("TEMP") or "/nonexistent-temp").resolve()).lower() in s


def _data_dir() -> Path:
    if os.environ.get("MCMGR_DATA"):
        return Path(os.environ["MCMGR_DATA"])
    # existing installs keep their world/config next to the code; new ones use the user profile
    # (the repo root counts too: apps/manager/ sits two levels below it in a source checkout)
    for legacy_base in (BASE, BASE.parent.parent):
        if (legacy_base / "server").exists() or (legacy_base / "manager_config.json").exists():
            return legacy_base
    # Earlier builds used %APPDATA%; keep using it if that is where the world already is.
    legacy = Path(os.environ.get("APPDATA") or "/nonexistent") / APP_NAME
    if (legacy / "server").exists() or (legacy / "manager_config.json").exists():
        return legacy
    # Plain folder in the user's home: easy to find, always NTFS on Windows, and not redirected
    # by the Microsoft Store Python sandbox (which silently relocates writes under AppData).
    return Path.home() / APP_NAME


DATA_DIR = _data_dir()
SERVER_DIR = DATA_DIR / "server"
BACKUP_DIR = DATA_DIR / "backups"
JAVA_DIR = DATA_DIR / "java"
TOOLS_DIR = BASE / "tools"
CONFIG_FILE = Path(os.environ.get("MCMGR_CONFIG") or DATA_DIR / "manager_config.json")

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
IS_WIN = sys.platform == "win32"

DEFAULT_CONFIG = {
    "java": "java",
    "ram_min": "1G",
    "ram_max": "2G",
    "version": "",
    "java_required": 0,
    "ssh_host": "",
    "ssh_user": "",
    "ssh_port": "22",
    "ssh_key": "",
    "ssh_remote_port": "25565",
    "ssh_bind": "0.0.0.0",
    "tunnel_autostart": False,
    "sync_enabled": False,
    "sync_user": "mcsync",
    "sync_key": "",
    "player_name": "",
    "proxy": "",               # optional SOCKS5 proxy (e.g. v2rayN 127.0.0.1:10808) for all VPS traffic
    "backup_keep": "5",        # automatic backups to keep
    "backup_max_gb": "5",      # space limit for all backups together
}


# ---------------------------------------------------------------- config
def load_config() -> dict:
    """Defaults < defaults.json shipped beside the code < the user's own saved settings."""
    def read(p: Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    shipped, user = read(BASE / "defaults.json"), read(CONFIG_FILE)
    cfg = dict(DEFAULT_CONFIG)
    for key in cfg:
        if key in user and user[key] != DEFAULT_CONFIG[key]:
            cfg[key] = user[key]      # the user changed it
        elif key in shipped:
            cfg[key] = shipped[key]   # still untouched: use the shipped preset
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- server.properties
def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    return props


def write_properties(path: Path, updates: dict[str, str]) -> None:
    """Update keys in place (keeping comments/unknown keys); append new ones."""
    lines = (
        path.read_text(encoding="utf-8", errors="replace").splitlines()
        if path.exists()
        else []
    )
    remaining = dict(updates)
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
                continue
        out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def eula_accepted() -> bool:
    p = SERVER_DIR / "eula.txt"
    return p.exists() and "eula=true" in p.read_text(errors="replace").lower()


def set_eula(accepted: bool) -> None:
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    (SERVER_DIR / "eula.txt").write_text(
        "# https://aka.ms/MinecraftEULA (set by Minecraft Server Manager)\n"
        f"eula={'true' if accepted else 'false'}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- ssh key
def resolve_key(path: str) -> str:
    """Expand ~ and treat relative paths as relative to the project folder."""
    p = Path(os.path.expanduser(path))
    return str(p if p.is_absolute() else BASE / p)


def usable_key(path: str) -> str:
    """Return a key path that ssh will accept.

    Windows OpenSSH refuses private keys other users can read. Drives formatted exFAT/FAT (USB
    sticks, many external disks) cannot store permissions at all, so on Windows we use a copy in
    the user's profile (always NTFS) locked to the current user. The copy is named by content hash,
    so an updated key gets a fresh copy and an existing read-only copy never needs overwriting.
    """
    key = Path(resolve_key(path))
    if not IS_WIN or not key.exists():
        return str(key)
    data = key.read_bytes()
    root = Path.home() / APP_NAME / ".keys"
    root.mkdir(parents=True, exist_ok=True)
    copy = root / f"{key.stem}-{hashlib.sha1(data).hexdigest()[:10]}"
    if not copy.exists():
        tmp = copy.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, copy)
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    user = f"{domain}\\{name}" if domain else name
    subprocess.run(["icacls", str(copy), "/inheritance:r", "/grant:r", f"{user}:R"], capture_output=True)
    return str(copy)


# ---------------------------------------------------------------- mc-hub (VPS invites)
HUB_PORT = 8766
HUB_FILE = DATA_DIR / "hub.json"        # owner code of the VPS hub; never shipped or committed


def own_key() -> str:
    """This PC's own SSH key (made on first use). The private half never leaves this PC."""
    d = DATA_DIR / "identity"
    key = d / "id_ed25519"
    if not key.exists():
        d.mkdir(parents=True, exist_ok=True)
        tool = find_tool("ssh-keygen")
        if not tool:
            raise RuntimeError("ssh-keygen was not found (Windows: Settings > Optional features > OpenSSH Client)")
        r = subprocess.run([tool, "-q", "-t", "ed25519", "-N", "", "-C", "mc-manager", "-f", str(key)],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"Could not create a key: {r.stderr.strip()}")
    return str(key)


def split_hub(text: str) -> tuple[str, int, str, str]:
    """'host[:port]/secret/fingerprint' -> (host, port, secret, fingerprint).

    The fingerprint (hex, start of the hub certificate's SHA-256) is how this app knows it is talking
    to the real hub and not something in between: the hub's certificate is self-signed."""
    text = text.strip()
    m = re.fullmatch(r"([\w.\-]+)(?::(\d{1,5}))?/([\w-]+)/([0-9a-fA-F]{16,64})", text)
    if not m:
        raise ValueError("That does not look right. Expected something like  1.2.3.4:8766/CODE/1a2b3c...  "
                         "(copy the whole line)")
    return m.group(1), int(m.group(2) or HUB_PORT), m.group(3), m.group(4).lower()


def _pinned_tls(sock: socket.socket, host: str, fp: str) -> ssl.SSLSocket:
    """TLS to the hub, trusting exactly the certificate whose fingerprint came with the code.

    The hub's certificate is self-signed, so the usual CA check cannot apply; instead the certificate is
    pinned by its SHA-256 (at least 64 bits of it) and checked before any secret is sent."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # replaced by the pin check right below
    tls = ctx.wrap_socket(sock, server_hostname=host)
    der = tls.getpeercert(binary_form=True) or b""
    if len(fp) < 16 or not hmac.compare_digest(hashlib.sha256(der).hexdigest()[:len(fp)], fp):
        tls.close()
        raise RuntimeError("The server's identity does not match the code (wrong address, a reinstalled hub, "
                           "or someone in between). Ask the owner for a fresh code.")
    return tls


def hub_call(host: str, port: int, method: str, path: str, body: dict | None = None,
             token: str = "", proxy: tuple[str, int] | None = None, timeout: float = 15, fp: str = "") -> dict:
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        raw = socks_connect(host, port, proxy, timeout=timeout) if proxy else \
            socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        conn.sock = _pinned_tls(raw, host, fp)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        resp = conn.getresponse()
        try:
            data = json.loads(resp.read() or b"{}")
        except ValueError:
            data = {}
    except (OSError, http.client.HTTPException) as e:
        raise RuntimeError(f"Could not reach the server at {host}:{port}. Is the address right, and is port "
                           f"{port} open in the VPS firewall? ({e})") from None
    finally:
        conn.close()
    if resp.status >= 400:
        raise RuntimeError(data.get("error") or f"The server answered {resp.status}")
    return data


def load_hub() -> dict:
    try:
        return json.loads(HUB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_hub(d: dict) -> None:
    HUB_FILE.parent.mkdir(parents=True, exist_ok=True)
    HUB_FILE.write_text(json.dumps(d), encoding="utf-8")


# ---------------------------------------------------------------- Java
def java_major(java: str = "java") -> int | None:
    """Return the installed Java major version, or None if not found."""
    exe = shutil.which(java) or (java if Path(java).exists() else None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr + r.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


# ---------------------------------------------------------------- downloads
def _open(url: str, timeout: int):
    # some hosts (Adoptium) reject urllib's default User-Agent
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": APP_NAME}), timeout=timeout)


def _get_json(url: str) -> dict:
    with _open(url, 30) as r:
        return json.load(r)


def list_release_versions(limit: int = 40) -> tuple[str, list[str]]:
    m = _get_json(MANIFEST_URL)
    rel = [v["id"] for v in m["versions"] if v["type"] == "release"][:limit]
    return m["latest"]["release"], rel


def _download(url: str, dest: Path, progress: Callable[[float], None] | None = None,
              sha1: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha1()
    with _open(url, 60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total * 100)
    if sha1 and h.hexdigest() != sha1:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Checksum mismatch - download corrupted, try again.")
    os.replace(tmp, dest)


def download_server(version: str, progress: Callable[[float], None] | None = None) -> int:
    """Download server.jar for `version` into SERVER_DIR. Returns required Java major."""
    m = _get_json(MANIFEST_URL)
    entry = next((v for v in m["versions"] if v["id"] == version), None)
    if not entry:
        raise RuntimeError(f"Unknown version {version}")
    meta = _get_json(entry["url"])
    srv = meta.get("downloads", {}).get("server")
    if not srv:
        raise RuntimeError(f"{version} has no server download")
    _download(srv["url"], SERVER_DIR / "server.jar", progress, srv["sha1"])
    return int(meta.get("javaVersion", {}).get("majorVersion", 0))


def _local_javas() -> list[str]:
    exe = "java.exe" if IS_WIN else "java"
    return sorted(str(p) for p in JAVA_DIR.glob(f"*/**/bin/{exe}")) if JAVA_DIR.exists() else []


def find_java(configured: str, need: int) -> str | None:
    """Return a java executable of at least `need`: the configured one, else one we installed."""
    for cand in [configured, *_local_javas()]:
        major = java_major(cand)
        if major is not None and major >= (need or 0):
            return cand
    return None


def install_java(major: int, progress: Callable[[float], None] | None = None) -> str:
    """Download a portable Temurin JRE (no admin rights needed) into DATA_DIR/java."""
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    osname = "windows" if IS_WIN else "linux" if sys.platform.startswith("linux") else None
    if not osname:
        raise RuntimeError("No automatic Java download for this OS - install Java manually.")
    url = (f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{osname}/{arch}/jre/hotspot/normal/eclipse")
    dest = JAVA_DIR / ("jre.zip" if IS_WIN else "jre.tar.gz")
    _download(url, dest, progress)
    target = JAVA_DIR / str(major)
    shutil.rmtree(target, ignore_errors=True)
    if IS_WIN:
        import zipfile
        with zipfile.ZipFile(dest) as z:
            z.extractall(target)
    else:
        import tarfile
        with tarfile.open(dest) as t:
            t.extractall(target)
    dest.unlink(missing_ok=True)
    found = find_java("", major)
    if not found:
        raise RuntimeError("Java download finished but no java executable was found.")
    if not IS_WIN:
        Path(found).chmod(0o755)
    return found


# ---------------------------------------------------------------- SOCKS5 proxy
# Some connections are throttled while a local proxy (v2rayN etc.) is fast. ssh/sftp/Java ignore
# the Windows proxy setting, so VPS traffic is sent through the proxy explicitly when configured.
def parse_proxy(text: str) -> tuple[str, int] | None:
    text = (text or "").strip()
    for prefix in ("socks5://", "socks://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    m = re.fullmatch(r"([\w.\-]+):(\d{1,5})", text)
    return (m.group(1), int(m.group(2))) if m else None


def _socks_request(host: str, port: int) -> bytes:
    try:
        addr = b"\x01" + socket.inet_aton(host)
    except OSError:
        addr = b"\x03" + bytes([len(host)]) + host.encode()
    return b"\x05\x01\x00" + addr + port.to_bytes(2, "big")


def _socks_reply_ok(head: bytes) -> bool:
    return len(head) >= 2 and head[0] == 5 and head[1] == 0


def socks_connect(host: str, port: int, proxy: tuple[str, int], timeout: float = 15) -> socket.socket:
    s = socket.create_connection(proxy, timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise OSError("The proxy did not accept a SOCKS5 connection")
    s.sendall(_socks_request(host, port))
    head = s.recv(4)
    if not _socks_reply_ok(head):
        raise OSError(f"The proxy could not reach {host}:{port}")
    s.recv({1: 4, 4: 16}.get(head[3], 0) + 2 if head[3] != 3 else s.recv(1)[0] + 2)
    s.settimeout(None)
    return s


async def socks_open(host: str, port: int, proxy: tuple[str, int]):
    """asyncio version: returns (reader, writer) connected to host:port through the proxy."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*proxy), 15)
    writer.write(b"\x05\x01\x00")
    if await reader.readexactly(2) != b"\x05\x00":
        raise OSError("The proxy did not accept a SOCKS5 connection")
    writer.write(_socks_request(host, port))
    head = await reader.readexactly(4)
    if not _socks_reply_ok(head):
        raise OSError(f"The proxy could not reach {host}:{port}")
    n = {1: 4, 4: 16}.get(head[3]) or (await reader.readexactly(1))[0]
    await reader.readexactly(n + 2)
    return reader, writer


def socks_pipe_main(host: str, port: str, proxy: str) -> None:
    """Entry point for ssh's ProxyCommand: bridge stdin/stdout to host:port through the proxy."""
    import threading
    s = socks_connect(host, int(port), parse_proxy(proxy))

    def upstream():
        try:
            while (d := sys.stdin.buffer.read1(65536)):
                s.sendall(d)
        finally:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    while (d := s.recv(65536)):
        sys.stdout.buffer.write(d)
        sys.stdout.buffer.flush()


def ssh_proxy_args(cfg: dict) -> list[str]:
    """Extra ssh/sftp options that route the connection through the configured proxy."""
    if not parse_proxy(cfg.get("proxy", "")):
        return []
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --socks-pipe %h %p {cfg["proxy"].strip()}'
    else:
        cmd = f'"{sys.executable}" "{BASE / "app.py"}" --socks-pipe %h %p {cfg["proxy"].strip()}'
    return ["-o", f"ProxyCommand={cmd}"]


def windows_proxy() -> str:
    """The proxy Windows itself is set to use (what v2rayN sets), or ''."""
    if not IS_WIN:
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                return ""
            server = str(winreg.QueryValueEx(k, "ProxyServer")[0])
    except OSError:
        return ""
    # "127.0.0.1:10808" or "http=...;https=...;socks=host:port"
    parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
    return parts.get("socks", server if "=" not in server else "")


def find_tool(name: str) -> str | None:
    local = TOOLS_DIR / (name + (".exe" if IS_WIN else ""))
    if local.exists():
        return str(local)
    return shutil.which(name)


# ---------------------------------------------------------------- processes
class ManagedProcess:
    """Async subprocess that streams output lines to a callback."""

    def __init__(self, on_line: Callable[[str], None],
                 on_exit: Callable[[int | None], None] | None = None):
        self.on_line = on_line
        self.on_exit = on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, args: list[str], cwd: Path | None = None,
                    env: dict[str, str] | None = None) -> None:
        if self.running:
            raise RuntimeError("Already running")
        full_env = {**os.environ, **(env or {})}
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None, env=full_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, limit=1 << 20,
        )
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                self.on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        finally:
            code = await self.proc.wait()
            if self.on_exit:
                self.on_exit(code)

    async def send(self, text: str) -> None:
        if self.running and self.proc and self.proc.stdin:
            self.proc.stdin.write((text + "\n").encode())
            await self.proc.stdin.drain()

    async def stop(self, graceful: str | None = None, timeout: float = 30) -> None:
        if not self.running or not self.proc:
            return
        try:
            if graceful:
                await self.send(graceful)
                await asyncio.wait_for(self.proc.wait(), timeout)
            else:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 10)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        if self._pump_task:
            await asyncio.gather(self._pump_task, return_exceptions=True)
