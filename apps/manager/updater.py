"""Self-update for the Windows .exe from GitHub releases.

Safe by design:
- Downloads only over HTTPS with normal certificate checks (through the configured SOCKS proxy if set).
- The exe must match the release's SHA256SUMS.txt, start with "MZ", and answer `--version` with the expected
  version before anything is replaced.
- Installs only while the server is stopped; the world, settings and backups live in the data folder and
  are never touched.
- The running exe is renamed to MinecraftManager.old.exe (Windows allows renaming a running program) and the
  new one takes its name. If the new version's backend dies before it confirms it is serving, the launcher
  puts the old exe back and starts it. The old exe is also kept for a manual "go back".
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import core

REPO = "iamhelitha/craftshare"
ASSET = "MinecraftManager.exe"
FEED = os.environ.get("MCMGR_UPDATE_FEED") or f"https://api.github.com/repos/{REPO}/releases/latest"
MARKER = core.DATA_DIR / "update.json"      # {"state": "pending"|"ok", "from", "to", "time"}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def old_exe() -> Path:
    return exe_path().with_name(exe_path().stem + ".old.exe")


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a: str, b: str) -> bool:
    """True if version a is newer than version b (unknown versions never count as newer)."""
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa and pb and pa > pb)


# ---------------------------------------------------------------- HTTPS download (direct or via SOCKS)
class _ProxyHTTPS(http.client.HTTPSConnection):
    """HTTPS through a SOCKS5 proxy. Certificate and host name are verified as usual."""

    def __init__(self, host: str, proxy: tuple[str, int], timeout: float):
        super().__init__(host, 443, timeout=timeout)
        self._proxy = proxy
        self._tls = ssl.create_default_context()

    def connect(self):
        raw = core.socks_connect(self.host, self.port, self._proxy, timeout=self.timeout)
        raw.settimeout(self.timeout)
        self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)


def https_get(url: str, proxy: tuple[str, int] | None = None, dest: Path | None = None,
              progress: Callable[[float], None] | None = None, limit: int = 300_000_000,
              timeout: float = 30) -> bytes:
    """GET with redirects. Returns the body, or writes it to `dest` (returns b'')."""
    for _ in range(6):
        u = urllib.parse.urlsplit(url)
        if u.scheme == "file" and os.environ.get("MCMGR_UPDATE_FEED"):          # local test feed only
            data = Path(urllib.request.url2pathname(u.path)).read_bytes()
            if dest is None:
                return data
            dest.write_bytes(data)
            return b""
        if u.scheme != "https" or not u.hostname:
            raise RuntimeError(f"Refusing to download from a non-HTTPS address: {url}")
        conn = (_ProxyHTTPS(u.hostname, proxy, timeout) if proxy else
                http.client.HTTPSConnection(u.hostname, 443, timeout=timeout, context=ssl.create_default_context()))
        try:
            conn.request("GET", u.path + (f"?{u.query}" if u.query else ""),
                         headers={"User-Agent": "CraftShare-updater", "Accept": "application/octet-stream, */*"})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                url = urllib.parse.urljoin(url, r.getheader("Location") or "")
                r.read()
                continue
            if r.status != 200:
                raise RuntimeError(f"Download failed: {u.hostname} answered {r.status}")
            total = int(r.getheader("Content-Length") or 0)
            if total > limit:
                raise RuntimeError("The download is unexpectedly large; stopped.")
            if dest is None:
                return r.read(limit)
            done = 0
            with open(dest, "wb") as f:
                while chunk := r.read(65536):
                    done += len(chunk)
                    if done > limit:
                        raise RuntimeError("The download is unexpectedly large; stopped.")
                    f.write(chunk)
                    if progress and total:
                        progress(done * 100 / total)
            if total and done != total:
                raise RuntimeError("The download was cut off. Try again.")
            return b""
        except (OSError, http.client.HTTPException) as e:
            raise RuntimeError(f"Could not reach {u.hostname}: {e}") from None
        finally:
            conn.close()
    raise RuntimeError("Too many redirects.")


# ---------------------------------------------------------------- check / download / swap
def check(proxy: tuple[str, int] | None = None) -> dict:
    data = json.loads(https_get(FEED, proxy, limit=2_000_000, timeout=15))
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    tag = data["tag_name"]
    return {"latest": tag.lstrip("v"), "tag": tag, "notes_url": data.get("html_url", ""),
            "published": data.get("published_at", ""), "exe_url": assets.get(ASSET, ""),
            "sums_url": assets.get("SHA256SUMS.txt", ""), "checked": time.time()}


def download_and_verify(info: dict, proxy, progress: Callable[[float], None], step: Callable[[str], None]) -> Path:
    """Fetch the new exe next to the current one and prove it is the published, working version."""
    if not frozen():
        raise RuntimeError("This copy runs from source code. Update it with  git pull  instead.")
    if not (info.get("exe_url") and info.get("sums_url")):
        raise RuntimeError("This release has no Windows download yet. Try again in a few minutes.")
    new = exe_path().with_name(exe_path().stem + ".new.exe")
    new.unlink(missing_ok=True)
    step("Checking the release...")
    sums = https_get(info["sums_url"], proxy, limit=100_000).decode("utf-8", "replace")
    want = next((ln.split()[0].lower() for ln in sums.splitlines()
                 if len(ln.split()) == 2 and ln.split()[1].lstrip("*") == ASSET), "")
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise RuntimeError("The release has no checksum for the app. Nothing was changed.")
    step(f"Downloading version {info['latest']}...")
    https_get(info["exe_url"], proxy, dest=new, progress=progress)
    progress(100)
    h = hashlib.sha256()
    with open(new, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != want or new.read_bytes()[:2] != b"MZ":
        new.unlink(missing_ok=True)
        raise RuntimeError("The download does not match the published release. Nothing was changed.")
    step("Testing the new version...")
    try:
        r = subprocess.run([str(new), "--version"], capture_output=True, text=True, timeout=180, env=clean_env(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.stdout.strip().splitlines()[-1:] == [info["latest"]]
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        new.unlink(missing_ok=True)
        raise RuntimeError("The new version did not start correctly, so it was not installed.")
    return new


def swap(new: Path, from_v: str, to_v: str) -> None:
    """Put the verified exe in place; keep the current one as MinecraftManager.old.exe."""
    cur, old = exe_path(), old_exe()
    old.unlink(missing_ok=True)
    os.replace(cur, old)                     # allowed while running
    try:
        os.replace(new, cur)
    except OSError:
        os.replace(old, cur)
        raise
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps({"state": "pending", "from": from_v, "to": to_v, "time": time.time()}))


def can_roll_back() -> bool:
    return frozen() and old_exe().exists()


def roll_back() -> None:
    """Put MinecraftManager.old.exe back in place (the current one is set aside and removed later)."""
    cur, old = exe_path(), old_exe()
    if not old.exists():
        raise RuntimeError("There is no previous version to go back to.")
    bad = cur.with_name(cur.stem + ".failed.exe")
    bad.unlink(missing_ok=True)
    os.replace(cur, bad)
    os.replace(old, cur)
    MARKER.unlink(missing_ok=True)


def pending() -> dict | None:
    try:
        m = json.loads(MARKER.read_text())
    except (OSError, ValueError):
        return None
    return m if m.get("state") == "pending" else None


def confirm() -> str | None:
    """Called by the backend once it is serving: the update worked. Returns the version it updated to."""
    m = pending()
    for leftover in (exe_path().with_name(exe_path().stem + ".failed.exe"),
                     exe_path().with_name(exe_path().stem + ".new.exe")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
    if m and m.get("to") == core.APP_VERSION:
        m["state"] = "ok"
        MARKER.write_text(json.dumps(m))
        return m["to"]
    return None


def clean_env() -> dict:
    """Environment for starting another copy of the exe as an independent program.

    A one-file PyInstaller exe passes _PYI_* / _MEIPASS2 variables to its children so they reuse its unpacked
    folder. A separate instance must not inherit them: that folder is deleted when this process exits."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("_PYI_") and k not in ("_MEIPASS2", "MCMGR_TOKEN")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def relaunch(wait_pid: int | None = None, reopen_port: int | None = None) -> None:
    """Start the program again in a new window (after this process exits)."""
    env = clean_env()
    env["MCMGR_NO_BROWSER"] = "1"
    if reopen_port:
        env["MCMGR_REOPEN_PORT"] = str(reopen_port)
    cmd = [str(exe_path())] if frozen() else [sys.executable, str(Path(__file__).with_name("app.py"))]
    if wait_pid:
        cmd += ["--wait-pid", str(wait_pid)]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(cmd, env=env, cwd=str(core.BASE), close_fds=True,
                         creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        subprocess.Popen(cmd, env=env, cwd=str(core.BASE), close_fds=True, creationflags=flags)


def wait_for_pid(pid: int, timeout: float = 90) -> None:
    """Wait until the previous instance has fully exited (so its port and files are free)."""
    if not core.IS_WIN:
        return
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
    if h:
        ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000))
        ctypes.windll.kernel32.CloseHandle(h)
