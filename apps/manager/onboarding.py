"""First-run checks and input rules.

`precheck` looks at this PC (tools, Java, RAM, disk, ports, internet, the VPS) and reports what will work,
what needs attention and what is broken, in plain words. The `valid_*` helpers are the single source of truth
for what the setup guide and the Setup page accept; the backend re-checks everything it receives.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import socket
import time
import urllib.request

import core

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")                 # same rule as the VPS hub
HOST_RE = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
IPV6_RE = re.compile(r"[0-9A-Fa-f:]{2,45}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
LEVEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,59}")


def valid_name(v: str) -> bool:
    return bool(NAME_RE.fullmatch(v or ""))


def valid_host(v: str) -> bool:
    return bool(HOST_RE.fullmatch(v) or IPV6_RE.fullmatch(v))


def valid_port(v: str, low: int = 1) -> bool:
    return str(v).isdigit() and low <= int(v) <= 65535


def valid_level_name(v: str) -> bool:
    """A folder name inside the server folder: no slashes, no '..', nothing hidden."""
    return bool(LEVEL_RE.fullmatch(v or "")) and ".." not in v and not v.endswith((" ", "."))


def mem_mb(v: str) -> int:
    m = re.fullmatch(r"(\d+)([MG])", (v or "").upper())
    return int(m.group(1)) * (1024 if m.group(2) == "G" else 1) if m else 0


def system_ram_mb() -> int:
    """Physical memory of this PC in MB (0 if unknown)."""
    try:
        if core.IS_WIN:
            class MEM(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong),
                            ("apage", ctypes.c_ulonglong), ("tvirt", ctypes.c_ulonglong),
                            ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
            m = MEM()
            m.length = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.total // (1024 * 1024))
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) // 1024
    except (OSError, ValueError, AttributeError):
        return 0


def recommended_ram_gb(total_mb: int) -> int:
    """About a third of the PC's memory, between 2 and 8 GB (what a small friends server needs)."""
    if not total_mb:
        return 4
    return max(2, min(8, round(total_mb / 1024 / 3)))


def _tcp_ms(host: str, port: int, proxy: tuple[str, int] | None = None, timeout: float = 4.0) -> int | None:
    start = time.time()
    try:
        s = core.socks_connect(host, port, proxy, timeout) if proxy else socket.create_connection((host, port), timeout)
        s.close()
        return int((time.time() - start) * 1000)
    except OSError:
        return None


def port_is_free(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def _item(id_: str, label: str, status: str, detail: str) -> dict:
    return {"id": id_, "label": label, "status": status, "detail": detail}


def precheck(cfg: dict, server_running: bool) -> dict:
    """status: ok | warn | fail. Nothing here changes anything on the PC."""
    items = []
    total = system_ram_mb()
    items.append(_item("ram", "Memory (RAM)", "ok" if total >= 4096 else "warn" if total else "warn",
                       f"{total / 1024:.0f} GB in this PC." + ("" if total >= 4096 else " 4 GB or more is comfortable.")
                       if total else "Could not read the amount of memory."))
    try:
        free = shutil.disk_usage(core.DATA_DIR if core.DATA_DIR.exists() else core.DATA_DIR.parent).free
    except OSError:
        free = 0
    gb = free / 1024 ** 3
    items.append(_item("disk", "Free disk space", "ok" if gb >= 5 else "warn" if gb >= 1.5 else "fail",
                       f"{gb:.1f} GB free where the server and backups are kept ({core.DATA_DIR})."
                       + ("" if gb >= 5 else " The server, Java and a few backups need about 3 GB." if gb >= 1.5
                          else " Free up some space first.")))
    need = int(cfg.get("java_required") or 0) or 21
    java = core.find_java(cfg.get("java", "java"), need)
    items.append(_item("java", "Java", "ok" if java else "ok",
                       f"Java {need}+ found." if java else f"Not installed. It downloads automatically (about 50 MB) "
                       "the first time you start the server; nothing for you to do."))
    for tool, why in (("ssh", "the tunnel"), ("sftp", "the shared world"), ("ssh-keygen", "joining with an invite")):
        found = core.find_tool(tool)
        items.append(_item(tool, f"{tool} (needed for {why})", "ok" if found else "warn",
                           "Found." if found else "Not found. Only needed for sharing with friends: Windows Settings > "
                           "Apps > Optional features > add 'OpenSSH Client'."))
    port = int(cfg.get("ssh_remote_port") or 25565)
    local_port = 25565
    if server_running:
        items.append(_item("port", f"Port {local_port} on this PC", "ok", "In use by the server you started."))
    else:
        free_port = port_is_free(local_port)
        items.append(_item("port", f"Port {local_port} on this PC", "ok" if free_port else "warn",
                           "Free." if free_port else "Something else already uses it (another Minecraft server?). "
                           "Close it, or change the server port in World settings > Advanced."))
    proxy = core.parse_proxy(cfg.get("proxy", ""))
    state = core.proxy_state(cfg)
    ms = _tcp_ms("piston-meta.mojang.com", 443, None, 5)
    if ms is not None:
        items.append(_item("internet", "Internet (Minecraft downloads)", "ok", f"Reachable ({ms} ms)."))
    elif state == "using" and _tcp_ms("piston-meta.mojang.com", 443, proxy, 6) is not None:
        items.append(_item("internet", "Internet (Minecraft downloads)", "warn",
                           "Only reachable through your proxy. Keep it running while downloading."))
    else:
        items.append(_item("internet", "Internet (Minecraft downloads)", "warn",
                           "Could not reach Mojang. Check your connection; you need it once to download the server."))
    host = cfg.get("ssh_host", "")
    if host and valid_host(host):
        direct = _tcp_ms(host, int(cfg.get("ssh_port") or 22), None, 5)
        via = _tcp_ms(host, int(cfg.get("ssh_port") or 22), proxy, 5) if state == "using" else None
        if direct is not None:
            items.append(_item("vps", "Your VPS", "ok", f"Reachable directly ({direct} ms)."))
        elif via is not None:
            items.append(_item("vps", "Your VPS", "ok", f"Reachable through your proxy ({via} ms)."))
        else:
            items.append(_item("vps", "Your VPS", "warn", "Could not reach it. It may be switched off, or the address is wrong."))
    return {"items": items, "ram_mb": total, "recommended_ram_gb": recommended_ram_gb(total),
            "ok": all(i["status"] != "fail" for i in items), "proxy_state": state}


def proxy_test(cfg: dict, proxy_text: str) -> dict:
    """Is this proxy running, and can it reach the VPS (or the internet)? Used by the setup guide."""
    p = core.parse_proxy(proxy_text)
    if not p:
        return {"alive": False, "msg": "That does not look like an address. Example: 127.0.0.1:10808"}
    if not core.proxy_alive(p, max_age=0):
        return {"alive": False, "msg": f"Nothing is listening on {proxy_text} right now (is the proxy app running?). "
                                       "That is fine: the normal connection is used until it is."}
    host, port = (cfg.get("ssh_host") or "piston-meta.mojang.com"), int(cfg.get("ssh_port") or 22) if cfg.get("ssh_host") else 443
    via = _tcp_ms(host, port, p, 6)
    direct = _tcp_ms(host, port, None, 6)
    if via is None:
        return {"alive": True, "msg": "The proxy is running but could not reach the server through it.", "via_ms": None,
                "direct_ms": direct}
    faster = direct is None or via < direct
    msg = f"Works: {via} ms through the proxy" + (f", {direct} ms directly." if direct is not None else ", directly it did not connect.")
    return {"alive": True, "msg": msg, "via_ms": via, "direct_ms": direct, "faster": faster}
