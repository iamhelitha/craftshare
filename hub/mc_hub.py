#!/usr/bin/env python3
"""mc-hub: tiny service that runs on the VPS and lets friends join without anyone touching a shell.

  sudo python3 mc_hub.py install     one-time setup (accounts, firewall, systemd service)
  sudo python3 mc_hub.py reset-token print a new owner code (use if you lost it)
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
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


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

    def log_message(self, *a):       # keep systemd's journal quiet
        pass

    def reply(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def throttled(self, record: bool = False) -> bool:
        ip, now = self.client_address[0], time.time()
        recent = [t for t in _fails.get(ip, []) if now - t < 60]
        if record:
            recent.append(now)
        _fails[ip] = recent
        return len(recent) >= 10

    def is_admin(self) -> bool:
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        st = load()
        ok = bool(tok and st["admin"] and hmac.compare_digest(sha(tok), st["admin"]))
        if not ok:
            self.throttled(record=True)
        return ok

    def do_GET(self):
        if self.path == "/info":
            return self.reply(200, {"mc_hub": HUB_VERSION, "port": MC_PORT})
        if self.path == "/admin/friends":
            if self.throttled() or not self.is_admin():
                return self.reply(403, {"error": "Not allowed"})
            st = load()
            friends = [{"name": n, "added": f["added"]} for n, f in sorted(st["friends"].items())]
            return self.reply(200, {"friends": friends, "legacy": legacy_count()})
        self.reply(404, {"error": "Not found"})

    def do_POST(self):
        b = self.read()
        with _lock:
            if self.path == "/enroll":
                return self.enroll(b)
            if self.path.startswith("/admin/"):
                if self.throttled() or not self.is_admin():
                    return self.reply(403, {"error": "Not allowed"})
                return self.admin(b)
        self.reply(404, {"error": "Not found"})

    def enroll(self, b: dict) -> None:
        if self.throttled():
            return self.reply(429, {"error": "Too many tries. Wait a minute."})
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
            self.throttled(record=True)
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


def serve() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", HUB_PORT), Handler)
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


def install() -> None:
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
    if shutil.which("ufw") and "Status: active" in sh("ufw", "status").stdout:
        sh("ufw", "allow", f"{MC_PORT}/tcp")
        sh("ufw", "allow", f"{HUB_PORT}/tcp")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(__file__, INSTALL_DIR / "mc_hub.py")
    Path("/etc/systemd/system/mc-hub.service").write_text(
        "[Unit]\nDescription=Minecraft manager hub\nAfter=network.target\n\n[Service]\n"
        f"ExecStart=/usr/bin/python3 {INSTALL_DIR}/mc_hub.py serve\nRestart=always\n"
        "MemoryMax=64M\n\n[Install]\nWantedBy=multi-user.target\n")
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
    print(f"If friends cannot connect, also open TCP {HUB_PORT} in your VPS provider's firewall panel.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "install":
        install()
    elif cmd == "reset-token":
        print(owner_code(make_token()))
    else:
        serve()
