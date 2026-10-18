#!/usr/bin/env python3
"""mc-hub: tiny service that runs on the VPS and lets friends join without anyone touching a shell.

  sudo python3 mc_hub.py install     one-time setup (accounts, systemd service); asks about the firewall
                                     (or pass --firewall / --no-firewall)
  sudo python3 mc_hub.py reset-token print a new owner code (use if you lost it)
  sudo python3 mc_hub.py uninstall   remove the service (keeps the world, accounts and friends)
  python3 mc_hub.py serve            run the service in the foreground (systemd does this)

Friends send an invite code + their PUBLIC key; the hub adds it to the two locked-down accounts
(mctunnel: may only open the Minecraft port, mcsync: may only use sftp inside /srv/mcworld).
Private keys never travel. Standard library only. Does not touch 3x-ui / Xray.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB_VERSION = 1
HUB_PORT = int(os.environ.get("MC_HUB_PORT", "8766"))
MC_PORT = 25565
INVITE_TTL = 24 * 3600
ROOT = Path(os.environ.get("MC_HUB_ROOT", "/"))          # tests point this at a temp folder
STATE_FILE = ROOT / "etc/mc-hub/state.json"
INSTALL_DIR = ROOT / "opt/mc-hub"
AUTH = {"mctunnel": ROOT / "home/mctunnel/.ssh/authorized_keys",
        "mcsync": ROOT / "home/mcsync/.ssh/authorized_keys"}
OPTIONS = {"mctunnel": f'restrict,port-forwarding,permitlisten="0.0.0.0:{MC_PORT}"',
           "mcsync": 'restrict,command="internal-sftp -d /srv/mcworld"'}
TAG = "mc-hub:"
KEY_RE = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{60,120}={0,2}")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_INVITES = 20                 # open invites at once
_lock = threading.Lock()


# ------------------------------------------------------------------ rate limits
class Limiter:
    """Keeps brute force and floods cheap for the VPS. All limits are per client IP unless noted.

    - any request:        REQ_LIMIT per minute, then 429 for the rest of that minute
    - wrong code / token: FAIL_LIMIT within FAIL_WINDOW blocks that IP for BLOCK_FOR seconds
    - all IPs together:   GLOBAL_FAILS wrong invite codes per hour pause /enroll (stops distributed guessing;
                          the owner's valid token still works)
    A 12-character invite has ~60 bits of randomness, so guessing is hopeless even without these limits.
    """
    REQ_LIMIT, FAIL_LIMIT, FAIL_WINDOW, BLOCK_FOR, GLOBAL_FAILS = 60, 5, 900, 900, 100
    MAX_TRACKED = 10000              # cap memory if someone sprays from many addresses

    def __init__(self):
        self.lock = threading.Lock()
        self.reqs: dict[str, list[float]] = {}
        self.fails: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}
        self.global_fails: list[float] = []

    def _prune(self, now: float) -> None:
        if len(self.reqs) + len(self.fails) + len(self.blocked) < self.MAX_TRACKED:
            return
        self.reqs = {ip: t for ip, t in self.reqs.items() if t and now - t[-1] < 60}
        self.fails = {ip: t for ip, t in self.fails.items() if t and now - t[-1] < self.FAIL_WINDOW}
        self.blocked = {ip: until for ip, until in self.blocked.items() if until > now}
        if len(self.reqs) + len(self.fails) + len(self.blocked) >= self.MAX_TRACKED:
            self.reqs.clear()        # still too many: forget request counts, keep blocks

    def check(self, ip: str) -> int:
        """Seconds the client must wait, or 0 if the request may go ahead."""
        now = time.time()
        with self.lock:
            self._prune(now)
            if self.blocked.get(ip, 0) > now:
                return int(self.blocked[ip] - now) + 1
            recent = [t for t in self.reqs.get(ip, []) if now - t < 60]
            recent.append(now)
            self.reqs[ip] = recent[-self.REQ_LIMIT - 1:]
            return 60 if len(recent) > self.REQ_LIMIT else 0

    def fail(self, ip: str, invite: bool = False) -> None:
        now = time.time()
        with self.lock:
            recent = [t for t in self.fails.get(ip, []) if now - t < self.FAIL_WINDOW] + [now]
            self.fails[ip] = recent
            if len(recent) >= self.FAIL_LIMIT:
                self.blocked[ip] = now + self.BLOCK_FOR
                print(f"blocked {ip} for {self.BLOCK_FOR}s after {len(recent)} failed attempts", flush=True)
            if invite:
                self.global_fails = [t for t in self.global_fails if now - t < 3600] + [now]

    def enroll_paused(self) -> bool:
        now = time.time()
        with self.lock:
            self.global_fails = [t for t in self.global_fails if now - t < 3600]
            return len(self.global_fails) >= self.GLOBAL_FAILS


LIMITS = Limiter()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ state
def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"admin": "", "invites": {}, "friends": {}}


def save(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def write_keys(st: dict) -> None:
    """Rewrite both authorized_keys: lines this hub did not create (the old shared keys) stay as they are."""
    for user, path in AUTH.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text().splitlines() if path.exists() else []
        keep = [ln for ln in old if ln.strip() and f" {TAG}" not in ln]
        mine = [f"{OPTIONS[user]} {f['key']} {TAG}{name}" for name, f in sorted(st["friends"].items())]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep + mine) + "\n")
        os.chmod(tmp, 0o644)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and ROOT == Path("/"):
            shutil.chown(tmp, "root", "root")
        os.replace(tmp, path)


def legacy_count() -> int:
    n = 0
    for path in AUTH.values():
        if path.exists():
            n += sum(1 for ln in path.read_text().splitlines() if ln.strip() and f" {TAG}" not in ln)
    return n


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


# ------------------------------------------------------------------ requests
class Handler(BaseHTTPRequestHandler):
    server_version = "mc-hub"
    timeout = 10                     # slow or idle clients are dropped (slowloris)

    def log_message(self, *a):       # keep systemd's journal quiet
        pass

    @property
    def ip(self) -> str:
        return self.client_address[0]

    def reply(self, status: int, data: dict, retry_after: int = 0) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def read(self) -> dict | None:
        """The JSON body, or None if it is too big or broken (never reads more than 4 KB)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= n <= 4096:
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def gate(self) -> bool:
        """Apply the rate limits; True if the request was refused."""
        wait = LIMITS.check(self.ip)
        if wait:
            self.reply(429, {"error": f"Too many requests. Try again in {wait} seconds."}, wait)
        return bool(wait)

    def is_admin(self) -> bool:
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        st = load()
        ok = bool(tok and st["admin"] and hmac.compare_digest(sha(tok), st["admin"]))
        if not ok:
            LIMITS.fail(self.ip)
        return ok

    def do_GET(self):
        if self.gate():
            return
        if self.path == "/info":
            return self.reply(200, {"mc_hub": HUB_VERSION, "port": MC_PORT})
        if self.path == "/admin/friends":
            if not self.is_admin():
                return self.reply(403, {"error": "Not allowed"})
            st = load()
            friends = [{"name": n, "added": f["added"]} for n, f in sorted(st["friends"].items())]
            return self.reply(200, {"friends": friends, "legacy": legacy_count()})
        self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if self.gate():
            return
        b = self.read()
        if b is None:
            return self.reply(400, {"error": "Bad request"})
        if self.path == "/enroll":
            if LIMITS.enroll_paused():
                return self.reply(503, {"error": "Joining is paused for a while (too many wrong codes). "
                                                 "Try again in an hour."}, 3600)
            with _lock:
                return self.enroll(b)
        if self.path.startswith("/admin/"):
            if not self.is_admin():
                return self.reply(403, {"error": "Not allowed"})
            with _lock:
                return self.admin(b)
        self.reply(404, {"error": "Not found"})

    def enroll(self, b: dict) -> None:
        code, name, key = (str(b.get(k, "")).strip() for k in ("code", "name", "pubkey"))
        key = " ".join(key.split()[:2])
        if not NAME_RE.fullmatch(name):
            return self.reply(400, {"error": "Name: letters, numbers, - and _ only (max 24)."})
        if not KEY_RE.fullmatch(key):
            return self.reply(400, {"error": "That is not a valid ed25519 public key."})
        st = load()
        now = time.time()
        st["invites"] = {h: i for h, i in st["invites"].items() if i["exp"] > now}
        inv = st["invites"].get(sha(code.upper().replace(" ", "")))
        if not inv:
            LIMITS.fail(self.ip, invite=True)
            return self.reply(403, {"error": "Invite code is wrong, used already, or expired."})
        if name in st["friends"]:
            return self.reply(409, {"error": f"The name '{name}' is already taken. Pick another."})
        del st["invites"][sha(code.upper().replace(" ", ""))]          # single use
        st["friends"][name] = {"key": key, "added": int(now)}
        save(st)
        write_keys(st)
        self.reply(200, {"ok": True, "ssh_user": "mctunnel", "sync_user": "mcsync", "ssh_port": 22,
                         "remote_port": MC_PORT, "hub": HUB_VERSION})

    def admin(self, b: dict) -> None:
        st = load()
        if self.path == "/admin/invite":
            st["invites"] = {h: i for h, i in st["invites"].items() if i["exp"] > time.time()}
            if len(st["invites"]) >= MAX_INVITES:
                return self.reply(429, {"error": f"{MAX_INVITES} invites are still unused. Wait for some to be used or expire."})
            code = new_code()
            st["invites"][sha(code)] = {"exp": time.time() + INVITE_TTL}
            save(st)
            return self.reply(200, {"code": code, "expires_in": INVITE_TTL})
        if self.path == "/admin/remove":
            name = str(b.get("name", ""))
            if name not in st["friends"]:
                return self.reply(404, {"error": "No such friend."})
            del st["friends"][name]
            save(st)
            write_keys(st)
            return self.reply(200, {"ok": True})
        if self.path == "/admin/remove-legacy":     # drop the old shared keys once everyone has an invite
            for path in AUTH.values():
                if path.exists():
                    lines = [ln for ln in path.read_text().splitlines() if f" {TAG}" in ln]
                    path.write_text("\n".join(lines) + ("\n" if lines else ""))
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "Not found"})


class Server(ThreadingHTTPServer):
    """At most MAX_CONN connections are handled at once; extra ones are closed straight away,
    so a flood cannot pile up threads and memory."""
    MAX_CONN = 32
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *a):
        super().__init__(*a)
        self.slots = threading.BoundedSemaphore(self.MAX_CONN)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve() -> None:
    srv = Server(("0.0.0.0", HUB_PORT), Handler)
    print(f"mc-hub {HUB_VERSION} listening on :{HUB_PORT}", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------ install
def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def make_token() -> str:
    st = load()
    token = secrets.token_urlsafe(24)
    st["admin"] = sha(token)
    save(st)
    return token


def owner_code(token: str) -> str:
    ip = sh("sh", "-c", "curl -s4 --max-time 5 https://ifconfig.me || hostname -I | cut -d' ' -f1", check=False).stdout.strip()
    return f"{ip}:{HUB_PORT}/{token}"


def open_firewall() -> None:
    """Open the game port and the invite port in whichever firewall is active (ufw or firewalld).
    ufw 'limit' also rate-limits new connections to the invite port (6 per 30 s per IP)."""
    if shutil.which("ufw") and "Status: active" in sh("ufw", "status", check=False).stdout:
        sh("ufw", "allow", f"{MC_PORT}/tcp")
        sh("ufw", "limit", f"{HUB_PORT}/tcp")
        print(f"ufw: allowed {MC_PORT}/tcp, rate-limited {HUB_PORT}/tcp.")
    elif shutil.which("firewall-cmd") and sh("firewall-cmd", "--state", check=False).returncode == 0:
        for port in (MC_PORT, HUB_PORT):
            sh("firewall-cmd", "--permanent", f"--add-port={port}/tcp")
        sh("firewall-cmd", "--reload")
        print(f"firewalld: opened {MC_PORT}/tcp and {HUB_PORT}/tcp.")
    else:
        print("No active firewall (ufw/firewalld) found, so nothing to change on this server.")
    print(f"If your VPS provider has a firewall in its web panel, open TCP {MC_PORT} and {HUB_PORT} there too.")


def install(firewall: bool = False) -> None:
    if os.geteuid() != 0:
        sys.exit("Run this as root:  sudo python3 mc_hub.py install")
    nologin = shutil.which("nologin") or "/usr/sbin/nologin"
    for user in ("mcsync", "mctunnel"):
        if sh("id", user, check=False).returncode:
            sh("useradd", "-m", "-s", nologin, user)
        sh("usermod", "-p", "*", user)
        sh("install", "-d", "-o", "root", "-g", "root", "-m", "755", f"/home/{user}/.ssh")
    sh("install", "-d", "-o", "mcsync", "-g", "mcsync", "-m", "700", "/srv/mcworld")
    cfgs = ["/etc/ssh/sshd_config", *map(str, Path("/etc/ssh/sshd_config.d").glob("*.conf"))]
    if not re.search(r"^\s*GatewayPorts\s+(yes|clientspecified)", "\n".join(Path(c).read_text() for c in cfgs),
                     re.I | re.M):
        with open("/etc/ssh/sshd_config", "a") as f:
            f.write("\nGatewayPorts clientspecified\n")
    sh("sshd", "-t")
    if sh("systemctl", "reload", "ssh", check=False).returncode:
        sh("systemctl", "reload", "sshd")
    if firewall:
        open_firewall()
    else:
        print(f"Firewall left unchanged. Friends need TCP {MC_PORT} (game) and {HUB_PORT} (invites) open.")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(__file__, INSTALL_DIR / "mc_hub.py")
    # Sandboxed: may only write its own state and the two authorized_keys folders, capped CPU/RAM/threads.
    Path("/etc/systemd/system/mc-hub.service").write_text(
        "[Unit]\nDescription=CraftShare hub (Minecraft invites)\nAfter=network.target\n\n[Service]\n"
        f"ExecStart=/usr/bin/python3 {INSTALL_DIR}/mc_hub.py serve\nRestart=always\nRestartSec=3\n"
        "MemoryMax=64M\nTasksMax=64\nCPUQuota=25%\n"
        "NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only\n"
        f"ReadWritePaths={STATE_FILE.parent} /home/mctunnel/.ssh /home/mcsync/.ssh\n"
        "\n[Install]\nWantedBy=multi-user.target\n")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    sh("systemctl", "daemon-reload")
    sh("systemctl", "enable", "--now", "mc-hub")
    sh("systemctl", "restart", "mc-hub")
    st = load()
    print("\nmc-hub is running. Your existing world files and keys were not touched.")
    if st["admin"]:
        print("(An owner code already exists. Use 'reset-token' if you lost it.)")
    else:
        print("\nYOUR OWNER CODE (paste it into the manager app, keep it private):\n")
        print("   " + owner_code(make_token()) + "\n")


def uninstall() -> None:
    if os.geteuid() != 0:
        sys.exit("Run this as root:  sudo python3 mc_hub.py uninstall")
    sh("systemctl", "disable", "--now", "mc-hub", check=False)
    Path("/etc/systemd/system/mc-hub.service").unlink(missing_ok=True)
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    sh("systemctl", "daemon-reload", check=False)
    print("mc-hub removed. The world in /srv/mcworld, the mctunnel/mcsync accounts and /etc/mc-hub were kept.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "install":
        if "--firewall" in sys.argv:
            fw = True
        elif "--no-firewall" in sys.argv or not sys.stdin.isatty():
            fw = False
        else:
            fw = input(f"Open TCP {MC_PORT} and {HUB_PORT} in this server's firewall? [y/N] ").strip().lower() in ("y", "yes")
        install(fw)
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "reset-token":
        print(owner_code(make_token()))
    else:
        serve()
