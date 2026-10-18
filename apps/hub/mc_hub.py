#!/usr/bin/env python3
"""CraftShare hub (mc-hub): the small service on your VPS that lets friends join with invite codes.

Commands (as root; after install, `mc-hub <command>` works from anywhere):
  mc-hub                     status: service, friends, invites, world store, blocked IPs, updates
  mc-hub invite              make an invite code for a friend
  mc-hub friends             list friends
  mc-hub remove NAME         remove one friend
  mc-hub remove-legacy       remove the old shared keys from the first setup
  mc-hub owner-code          make a new owner code (the old one stops working)
  mc-hub logs                recent service log
  mc-hub require-app [X.Y.Z|off]  minimum desktop app version allowed to host
  mc-hub update [vX.Y.Z]     update to the latest (or a given) release; checksum-verified, rolls back on failure
  mc-hub uninstall           remove everything, after offering a download of the world backup
  python3 mc_hub.py install [--firewall | --no-firewall]    first-time setup (install.sh runs this)
  python3 mc_hub.py serve    run the service (systemd does this)

The network-facing service uses only the Python standard library and speaks HTTPS with a self-signed
certificate that the desktop app pins (its fingerprint is part of every code). The commands use `rich`
when it is available. Private keys never travel; friends' keys can only open the Minecraft port or use
sftp inside /srv/mcworld. Nothing here touches 3x-ui / Xray.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_VERSION = "dev"          # stamped with the release version by the release build
HUB_VERSION = 2              # protocol version
REPO = "iamhelitha/craftshare"
HUB_PORT = int(os.environ.get("MC_HUB_PORT", "8766"))
MC_PORT = 25565
INVITE_TTL = 24 * 3600
MAX_INVITES = 20             # open invites at once
EXPORT_TTL = 3600            # world backup download link lifetime (uninstall)
EXPORT_DOWNLOADS = 3         # downloads allowed per link
FP_LEN = 20                  # hex chars of the certificate fingerprint carried in codes (80 bits)

ROOT = Path(os.environ.get("MC_HUB_ROOT", "/"))          # tests point this at a temp folder
TEST_MODE = ROOT != Path("/")
STATE_DIR = ROOT / "var/lib/mc-hub"
STATE_FILE = STATE_DIR / "state.json"
RUNTIME_FILE = STATE_DIR / "runtime.json"
TLS_DIR = STATE_DIR / "tls"
CERT, KEY = TLS_DIR / "cert.pem", TLS_DIR / "key.pem"
EXPORT_DIR = STATE_DIR / "export"
INSTALL_DIR = ROOT / "opt/mc-hub"
BIN = ROOT / "usr/local/bin/mc-hub"
UNIT = ROOT / "etc/systemd/system/mc-hub.service"
SSHD_CONFIG = ROOT / "etc/ssh/sshd_config"
WORLD_DIR = ROOT / "srv/mcworld"
USERS = ("mctunnel", "mcsync")
AUTH = {u: ROOT / f"home/{u}/.ssh/authorized_keys" for u in USERS}
OPTIONS = {"mctunnel": f'restrict,port-forwarding,permitlisten="0.0.0.0:{MC_PORT}"',
           "mcsync": 'restrict,command="internal-sftp -d /srv/mcworld"'}
TAG = "mc-hub:"
GATEWAY_MARK = "# added by craftshare mc-hub (removed on uninstall)"
KEY_RE = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{60,120}={0,2}")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ state
def blank_state() -> dict:
    return {"admin": "", "invites": {}, "friends": {}, "export": None, "public_ip": "",
            "installed": {"firewall": [], "gateway_added": False, "pkg_rich": ""}}


def load() -> dict:
    st = blank_state()
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        st.update(data)
    st["installed"] = {**blank_state()["installed"], **(st.get("installed") or {})}
    return st


def save(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


_tlock = threading.Lock()


@contextmanager
def locked():
    """One writer at a time, across the service's threads and the mc-hub command."""
    with _tlock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:          # Windows (tests): the thread lock is enough
            yield
            return
        with open(STATE_DIR / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def write_keys(st: dict) -> None:
    """Rewrite both authorized_keys: lines this hub did not create (the old shared keys) stay as they are."""
    for user, path in AUTH.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text().splitlines() if path.exists() else []
        keep = [ln for ln in old if ln.strip() and f" {TAG}" not in ln]
        mine = [f"{OPTIONS[user]} {f['key']} {TAG}{name}" for name, f in sorted(st["friends"].items())]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep + mine) + "\n")
        os.chmod(tmp, 0o644)             # sshd reads it as the account user
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


def open_invites(st: dict) -> dict:
    now = time.time()
    return {h: i for h, i in st["invites"].items() if i["exp"] > now}


# ------------------------------------------------------------------ TLS
def make_cert() -> None:
    """Self-signed certificate. Clients pin its fingerprint (carried in every code), so no CA is needed."""
    if CERT.exists() and KEY.exists():
        return
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_DIR, 0o700)
    tool = shutil.which("openssl")
    if not tool:
        raise SystemExit("openssl is needed to make the HTTPS certificate (apt install openssl).")
    r = subprocess.run([tool, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "3650", "-subj", "/CN=craftshare-hub",
                        "-keyout", str(KEY), "-out", str(CERT)],
                       capture_output=True, text=True, env={**os.environ, "MSYS2_ARG_CONV_EXCL": "*"})
    if r.returncode:
        raise SystemExit(f"Could not make the certificate: {r.stderr.strip()}")
    os.chmod(KEY, 0o600)


def fingerprint() -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(CERT.read_text())).hexdigest()


# ------------------------------------------------------------------ rate limits
class Limiter:
    """Keeps brute force and floods cheap for the VPS. All limits are per client IP unless noted.

    - any request:        REQ_LIMIT per minute, then 429 for the rest of that minute
    - wrong code / token: FAIL_LIMIT within FAIL_WINDOW blocks that IP for BLOCK_FOR seconds
                          (invite codes, owner token, backup download links)
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
        self.total = 0

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
            self.total += 1
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

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            blocked = sorted(((ip, int(u - now)) for ip, u in self.blocked.items() if u > now), key=lambda x: -x[1])
            return {"requests": self.total, "blocked": blocked[:50], "blocked_count": len(blocked),
                    "failed_15min": sum(len([t for t in v if now - t < self.FAIL_WINDOW]) for v in self.fails.values()),
                    "invite_guesses_hour": len([t for t in self.global_fails if now - t < 3600]),
                    "enroll_paused": len([t for t in self.global_fails if now - t < 3600]) >= self.GLOBAL_FAILS}


LIMITS = Limiter()
EXPORT_SLOTS = threading.BoundedSemaphore(2)      # backup downloads at once


# ------------------------------------------------------------------ requests
class Handler(BaseHTTPRequestHandler):
    server_version = "mc-hub"
    sys_version = ""                 # do not advertise the Python version
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
            return self.reply(200, {"mc_hub": HUB_VERSION})
        if self.path == "/admin/friends":
            if not self.is_admin():
                return self.reply(403, {"error": "Not allowed"})
            st = load()
            friends = [{"name": n, "added": f["added"]} for n, f in sorted(st["friends"].items())]
            return self.reply(200, {"friends": friends, "legacy": legacy_count(), "version": APP_VERSION,
                                    "invites": len(open_invites(st))})
        m = re.fullmatch(r"/export/([A-Za-z0-9_-]{20,64})", self.path)
        if m:
            return self.export(m.group(1))
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
            with locked():
                return self.enroll(b)
        if self.path.startswith("/admin/"):
            if not self.is_admin():
                return self.reply(403, {"error": "Not allowed"})
            with locked():
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
        st["invites"] = open_invites(st)
        h = sha(code.upper().replace(" ", ""))
        if h not in st["invites"]:
            LIMITS.fail(self.ip, invite=True)
            return self.reply(403, {"error": "Invite code is wrong, used already, or expired."})
        if name in st["friends"]:
            return self.reply(409, {"error": f"The name '{name}' is already taken. Pick another."})
        del st["invites"][h]                                # single use
        st["friends"][name] = {"key": key, "added": int(time.time())}
        save(st)
        write_keys(st)
        print(f"friend joined: {name}", flush=True)
        self.reply(200, {"ok": True, "ssh_user": "mctunnel", "sync_user": "mcsync", "ssh_port": 22,
                         "remote_port": MC_PORT, "hub": HUB_VERSION})

    def admin(self, b: dict) -> None:
        st = load()
        if self.path == "/admin/invite":
            st["invites"] = open_invites(st)
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
            remove_legacy()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "Not found"})

    def export(self, token: str) -> None:
        """World backup download offered during uninstall: secret link, 1 hour, 3 downloads, 2 at a time."""
        st = load()
        ex = st.get("export") or {}
        name = str(ex.get("file", ""))
        ok = bool(ex.get("hash") and hmac.compare_digest(sha(token), ex["hash"]) and ex.get("exp", 0) > time.time()
                  and ex.get("left", 0) > 0 and re.fullmatch(r"[\w.-]+\.zip", name) and (EXPORT_DIR / name).is_file())
        if not ok:
            LIMITS.fail(self.ip)
            return self.reply(404, {"error": "Not found"})
        if not EXPORT_SLOTS.acquire(blocking=False):
            return self.reply(429, {"error": "Another download is running. Try again in a minute."}, 60)
        try:
            with locked():
                st = load()
                if not st.get("export") or st["export"].get("left", 0) <= 0:
                    return self.reply(404, {"error": "Not found"})
                st["export"]["left"] -= 1
                st["export"]["downloads"] = st["export"].get("downloads", 0) + 1
                save(st)
            path = EXPORT_DIR / name
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Connection", "close")
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
            print(f"backup downloaded by {self.ip}", flush=True)
        except OSError:
            pass
        finally:
            EXPORT_SLOTS.release()
            self.close_connection = True


def remove_legacy() -> None:
    for path in AUTH.values():
        if path.exists():
            lines = [ln for ln in path.read_text().splitlines() if f" {TAG}" in ln]
            path.write_text("\n".join(lines) + ("\n" if lines else ""))


class Server(ThreadingHTTPServer):
    """At most MAX_CONN connections are handled at once; extra ones are closed straight away, so a
    flood cannot pile up threads and memory. The TLS handshake runs in the worker thread with a
    timeout, so a slow handshake cannot stall accepting other connections."""
    MAX_CONN = 32
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, addr, handler, ctx: ssl.SSLContext | None = None):
        super().__init__(addr, handler)
        self.ctx = ctx
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
            if self.ctx:
                request.settimeout(Handler.timeout)
                try:
                    request = self.ctx.wrap_socket(request, server_side=True)
                except (OSError, ssl.SSLError):
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def runtime_writer() -> None:
    """Every 15 s, write counters for `mc-hub status` (the command cannot see this process's memory)."""
    started = int(time.time())
    while True:
        snap = LIMITS.snapshot()
        snap.update(started=started, version=APP_VERSION, pid=os.getpid(), updated=int(time.time()))
        try:
            tmp = RUNTIME_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap))
            os.chmod(tmp, 0o600)
            os.replace(tmp, RUNTIME_FILE)
        except OSError:
            pass
        time.sleep(15)


def serve() -> None:
    ctx = None
    if CERT.exists() and KEY.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, KEY)
    elif not TEST_MODE:
        raise SystemExit("No HTTPS certificate found. Run the installer again.")
    srv = Server(("0.0.0.0", HUB_PORT), Handler, ctx)
    threading.Thread(target=runtime_writer, daemon=True).start()
    print(f"mc-hub {APP_VERSION} (protocol {HUB_VERSION}) listening on :{HUB_PORT}"
          f"{' with HTTPS' if ctx else ''}", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------ terminal output (rich if available)
try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    console = Console()
    RICH = True
except ImportError:
    RICH = False


def plain(text: str) -> str:
    return re.sub(r"\[/?[a-z0-9 #_.-]*\]", "", text)


def say(text: str = "") -> None:
    if RICH:
        console.print(text)
    else:
        print(plain(text))


def ask_yes(question: str, default: bool = False) -> bool:
    if RICH:
        return Confirm.ask(question, default=default)
    ans = input(f"{plain(question)} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not ans else ans in ("y", "yes")


def ask_text(question: str) -> str:
    return Prompt.ask(question) if RICH else input(f"{plain(question)}: ")


def panel(title: str, lines: list[str]) -> None:
    if RICH:
        console.print(Panel("\n".join(lines), title=f"[bold]{title}[/]", title_align="left", expand=False))
    else:
        say(f"== {title} ==")
        for ln in lines:
            say("  " + ln)


def table(title: str, cols: list[str], rows: list[list[str]], empty: str = "none") -> None:
    if not rows:
        say(f"[bold]{title}:[/] [dim]{empty}[/]")
        return
    if RICH:
        t = Table(title=title, title_justify="left", box=box.SIMPLE_HEAVY)
        for c in cols:
            t.add_column(c)
        for r in rows:
            t.add_row(*r)
        console.print(t)
    else:
        say(f"{title}:")
        for r in rows:
            say("  " + " | ".join(r))


def size_text(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago" if s < 172800 \
        else time.strftime("%Y-%m-%d", time.localtime(ts))


# ------------------------------------------------------------------ helpers for the commands
def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def require_root() -> None:
    if not TEST_MODE and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise SystemExit("Run this as root (sudo -i first, or prefix the command with sudo).")


def fetch(url: str, timeout: float = 30, limit: int = 20_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "craftshare-mc-hub"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # HTTPS with normal certificate checks
        return r.read(limit)


def latest_tag() -> str:
    return json.loads(fetch(f"https://api.github.com/repos/{REPO}/releases/latest", 8))["tag_name"]


def public_ip(st: dict) -> str:
    if not st.get("public_ip"):
        try:
            st["public_ip"] = fetch("https://api.ipify.org", 5).decode().strip()
        except OSError:
            st["public_ip"] = (sh("hostname", "-I", check=False).stdout.split() or ["YOUR_VPS_IP"])[0]
    return st["public_ip"]


def code_for(st: dict, secret: str) -> str:
    return f"{public_ip(st)}:{HUB_PORT}/{secret}/{fingerprint()[:FP_LEN]}"


def service_state() -> str:
    if not shutil.which("systemctl"):
        return "unknown"
    return sh("systemctl", "is-active", "mc-hub", check=False).stdout.strip() or "unknown"


def make_token(st: dict) -> str:
    token = secrets.token_urlsafe(24)
    st["admin"] = sha(token)
    return token


# ------------------------------------------------------------------ commands
def cmd_status(_args) -> None:
    require_root()
    st = load()
    try:
        rt = json.loads(RUNTIME_FILE.read_text())
    except (OSError, ValueError):
        rt = {}
    state = service_state()
    try:
        latest = latest_tag()
    except (OSError, ValueError, KeyError):
        latest = ""
    if not latest:
        upd = "[dim]could not check for updates[/]"
    elif latest.lstrip("v") == APP_VERSION:
        upd = "[green]up to date[/]"
    else:
        upd = f"[yellow]{latest} is available: run  mc-hub update[/]"
    fp = fingerprint()[:FP_LEN] if CERT.exists() else "missing"
    colour = "green" if state == "active" else "red"
    lines = [f"Service    [{colour}]{state}[/]" + (f" since {ago(rt['started'])}" if rt.get("started") else ""),
             f"Version    {APP_VERSION}  ({upd})",
             f"Address    https://{public_ip(st)}:{HUB_PORT}   certificate {fp}",
             f"Requests   {rt.get('requests', 0)} since start"]
    if rt.get("enroll_paused"):
        lines.append("[red]Joining is paused: too many wrong invite codes in the last hour.[/]")
    panel("CraftShare hub", lines)

    table("Friends", ["Name", "Joined"],
          [[n, time.strftime("%Y-%m-%d", time.localtime(f["added"]))] for n, f in sorted(st["friends"].items())],
          "nobody yet (make one with: mc-hub invite)")
    inv = open_invites(st)
    if inv:
        soon = min(i["exp"] for i in inv.values())
        say(f"[bold]Open invites:[/] {len(inv)} (next expires in {int((soon - time.time()) / 3600)} h)")
    if legacy_count():
        say(f"[yellow]Old shared keys still active: {legacy_count()}[/] (remove with: mc-hub remove-legacy)")

    rows = []
    if WORLD_DIR.exists():
        for p in sorted(WORLD_DIR.iterdir()):
            if p.is_file():
                rows.append([p.name, size_text(p.stat().st_size), ago(p.stat().st_mtime)])
    table("Shared world on this VPS", ["File", "Size", "Changed"], rows, "empty")
    lock = WORLD_DIR / "lock"
    if lock.is_dir():
        try:
            info = json.loads((lock / "lock.json").read_text())
            hb = json.loads((lock / "heartbeat.json").read_text()) if (lock / "heartbeat.json").exists() else {}
            beat = hb.get("time") or info.get("since", 0)
            say(f"[bold]Hosting now:[/] {info.get('holder', '?')} (last heartbeat {ago(beat)})")
        except (OSError, ValueError):
            say("[bold]Hosting now:[/] someone (lock present)")
    else:
        say("[bold]Hosting now:[/] nobody")
    need = read_policy().get("min_app")
    say(f"[bold]App version needed to host:[/] {need} or newer" if need else "[bold]App version needed to host:[/] any")

    sec = [f"Failed attempts (15 min)     {rt.get('failed_15min', 0)}",
           f"Wrong invite codes (1 hour)  {rt.get('invite_guesses_hour', 0)}",
           f"Blocked IPs right now        {rt.get('blocked_count', 0)}"]
    panel("Security", sec)
    table("Blocked IPs", ["IP", "Unblocks in"], [[ip, f"{s // 60} min"] for ip, s in rt.get("blocked", [])], "none")
    fw = st["installed"]["firewall"]
    say("[bold]Firewall rules added by the hub:[/] " + (", ".join(f"{r[0]} {r[1]} {r[2]}" for r in fw) if fw else "none"))


def cmd_friends(_args) -> None:
    require_root()
    st = load()
    table("Friends", ["Name", "Joined"],
          [[n, time.strftime("%Y-%m-%d %H:%M", time.localtime(f["added"]))] for n, f in sorted(st["friends"].items())],
          "nobody yet")


def cmd_invite(_args) -> None:
    require_root()
    with locked():
        st = load()
        st["invites"] = open_invites(st)
        if len(st["invites"]) >= MAX_INVITES:
            raise SystemExit(f"{MAX_INVITES} invites are still unused. Wait for some to be used or expire.")
        code = new_code()
        st["invites"][sha(code)] = {"exp": time.time() + INVITE_TTL}
        text = code_for(st, code)
        save(st)
    panel("Invite code (works once, for 24 hours)", [f"[bold green]{text}[/]", "",
          "Your friend pastes it in the app: Setup > Friends & access > Join a server."])


def cmd_remove(args) -> None:
    require_root()
    if not args:
        raise SystemExit("Usage: mc-hub remove NAME")
    name = args[0]
    with locked():
        st = load()
        if name not in st["friends"]:
            raise SystemExit(f"No friend called {name}. See: mc-hub friends")
        if not ask_yes(f"Remove {name}? They will no longer be able to host or load the world."):
            return
        del st["friends"][name]
        save(st)
        write_keys(st)
    say(f"[green]{name} removed.[/]")


def cmd_remove_legacy(_args) -> None:
    require_root()
    n = legacy_count()
    if not n:
        say("No old shared keys left.")
        return
    if ask_yes(f"Remove {n} old shared key line(s)? Anyone still using the old zip with keys inside stops working."):
        with locked():
            remove_legacy()
        say("[green]Old shared keys removed.[/]")


def cmd_owner_code(_args) -> None:
    require_root()
    if not ask_yes("Make a new owner code? The old one stops working (paste the new one into your app)."):
        return
    with locked():
        st = load()
        token = make_token(st)
        text = code_for(st, token)
        save(st)
    panel("Owner code (keep it private)", [f"[bold green]{text}[/]", "",
          "Paste it in the app: Setup > Friends & access > I own the VPS."])


POLICY = WORLD_DIR / "policy.json"


def read_policy() -> dict:
    try:
        return json.loads(POLICY.read_text())
    except (OSError, ValueError):
        return {}


def cmd_require_app(args) -> None:
    """Minimum desktop app version allowed to host (older apps are asked to update first)."""
    require_root()
    if not args:
        cur = read_policy().get("min_app")
        say(f"Hosting requires app version {cur} or newer." if cur else "Any app version may host.")
        say("Change it with:  mc-hub require-app 0.4.0    or    mc-hub require-app off")
        return
    if args[0] == "off":
        POLICY.unlink(missing_ok=True)
        say("[green]Any app version may host now.[/]")
        return
    if not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        raise SystemExit("Give a version like 0.4.0 (or 'off').")
    if not ask_yes(f"Require app version {args[0]} or newer to host? Friends on older versions will be "
                   "asked to update before they can start the server.", True):
        return
    WORLD_DIR.mkdir(parents=True, exist_ok=True)
    POLICY.write_text(json.dumps({"min_app": args[0], "set": int(time.time())}))
    os.chmod(POLICY, 0o644)
    say(f"[green]Hosting now requires app version {args[0]} or newer.[/]")


def cmd_logs(args) -> None:
    require_root()
    n = args[0] if args and args[0].isdigit() else "60"
    print(sh("journalctl", "-u", "mc-hub", "-n", n, "--no-pager", check=False).stdout)


def healthy(timeout: float = 20) -> bool:
    ctx = ssl.create_default_context(cafile=str(CERT))   # trust exactly our own certificate
    ctx.check_hostname = False                            # it is issued to "craftshare-hub", not 127.0.0.1
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:{HUB_PORT}/info", timeout=3, context=ctx) as r:
                if json.loads(r.read()).get("mc_hub"):
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def cmd_update(args) -> None:
    """Download a release's mc_hub.py, verify it, swap it in, restart, health-check, roll back on failure."""
    require_root()
    tag = next((a for a in args if a.startswith("v")), "") or latest_tag()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"Not a release tag: {tag}")
    new_ver = tag[1:]
    if new_ver == APP_VERSION and "--force" not in args:
        say(f"[green]Already on {APP_VERSION}.[/]")
        return
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    say(f"Downloading mc-hub {tag} from github.com/{REPO} ...")
    code = fetch(f"{base}/mc_hub.py")
    sums = fetch(f"{base}/SHA256SUMS.txt").decode()
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.split()[-1:] in (["mc_hub.py"], ["*mc_hub.py"])), "")
    if not want or want != hashlib.sha256(code).hexdigest():
        raise SystemExit("Checksum mismatch: the download is not what the release published. Nothing was changed.")
    if f'APP_VERSION = "{new_ver}"'.encode() not in code:
        raise SystemExit("The downloaded file does not carry the expected version. Nothing was changed.")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    cur, bak, new = INSTALL_DIR / "mc_hub.py", INSTALL_DIR / "mc_hub.py.bak", INSTALL_DIR / "mc_hub.py.new"
    new.write_bytes(code)
    if sh(sys.executable, "-m", "py_compile", str(new), check=False).returncode:
        new.unlink()
        raise SystemExit("The downloaded file does not compile. Nothing was changed.")
    if not ask_yes(f"Update mc-hub {APP_VERSION} -> {new_ver}? Friends, invites and the world are kept.", True):
        new.unlink()
        return
    shutil.copy2(cur, bak)
    os.replace(new, cur)
    r = sh("/usr/bin/python3", str(cur), "setup-service", check=False)
    if r.returncode == 0 and healthy():
        say(f"[green]Updated to {new_ver}. The previous version is kept as {bak}.[/]")
        return
    say(f"[red]The new version did not start ({(r.stderr or 'no answer from the service').strip()[-300:]}). "
        "Rolling back...[/]")
    shutil.copy2(bak, cur)
    sh("/usr/bin/python3", str(cur), "setup-service", check=False)
    say("[green]Back on the previous version.[/]" if healthy() else
        "[red]Rollback did not start either. See: mc-hub logs[/]")


# ---- install / service setup
def gateway_set() -> bool:
    cfgs = [SSHD_CONFIG, *(SSHD_CONFIG.parent / "sshd_config.d").glob("*.conf")]
    text = "\n".join(p.read_text() for p in cfgs if p.exists())
    return bool(re.search(r"^\s*GatewayPorts\s+(yes|clientspecified)", text, re.I | re.M))


def reload_sshd() -> None:
    if sh("sshd", "-t", check=False).returncode:
        raise SystemExit("sshd configuration test failed; nothing reloaded. Check /etc/ssh/sshd_config.")
    if sh("systemctl", "reload", "ssh", check=False).returncode:
        sh("systemctl", "reload", "sshd", check=False)


def open_firewall() -> list[list[str]]:
    """Open the game port and the invite port in the active firewall. Returns only the rules it added,
    so uninstall can remove exactly those. ufw 'limit' also rate-limits new connections to the invite
    port (6 per 30 s per IP)."""
    added = []
    if shutil.which("ufw") and "Status: active" in sh("ufw", "status", check=False).stdout:
        status = sh("ufw", "status").stdout
        for action, port in (("allow", f"{MC_PORT}/tcp"), ("limit", f"{HUB_PORT}/tcp")):
            if re.search(rf"^{re.escape(port)}\s", status, re.M):
                say(f"ufw already has a rule for {port}; leaving it as it is.")
                continue
            sh("ufw", action, port)
            added.append(["ufw", action, port])
            say(f"ufw: {action} {port}")
    elif shutil.which("firewall-cmd") and sh("firewall-cmd", "--state", check=False).returncode == 0:
        for port in (f"{MC_PORT}/tcp", f"{HUB_PORT}/tcp"):
            if sh("firewall-cmd", f"--query-port={port}", check=False).returncode == 0:
                say(f"firewalld already allows {port}; leaving it as it is.")
                continue
            sh("firewall-cmd", "--permanent", f"--add-port={port}")
            added.append(["firewalld", "port", port])
        sh("firewall-cmd", "--reload", check=False)
    else:
        say("No active firewall (ufw/firewalld) found, so nothing to change on this server.")
    say(f"If your VPS provider has a firewall in its web panel, open TCP {MC_PORT} and {HUB_PORT} there too.")
    return added


def close_firewall(rules: list[list[str]]) -> None:
    for kind, action, port in rules:
        if kind == "ufw":
            sh("ufw", "delete", action, port, check=False)
        elif kind == "firewalld":
            sh("firewall-cmd", "--permanent", f"--remove-port={port}", check=False)
        say(f"  firewall: removed {kind} {action} {port}")
    if any(r[0] == "firewalld" for r in rules):
        sh("firewall-cmd", "--reload", check=False)


def install_rich(st: dict) -> None:
    """`rich` makes the mc-hub command readable. The service itself never imports it."""
    def has_rich(py: str) -> bool:
        return Path(py).exists() and sh(py, "-c", "import rich", check=False).returncode == 0
    if has_rich("/usr/bin/python3"):
        return
    if shutil.which("apt-get"):
        sh("apt-get", "update", "-qq", check=False)
        if sh("apt-get", "install", "-y", "-qq", "python3-rich", check=False).returncode == 0 and has_rich("/usr/bin/python3"):
            st["installed"]["pkg_rich"] = "apt-get"
            return
    if shutil.which("dnf") and sh("dnf", "install", "-y", "-q", "python3-rich", check=False).returncode == 0 \
            and has_rich("/usr/bin/python3"):
        st["installed"]["pkg_rich"] = "dnf"
        return
    venv = INSTALL_DIR / "venv"
    if sh("/usr/bin/python3", "-m", "venv", str(venv), check=False).returncode == 0 and \
            sh(str(venv / "bin/pip"), "install", "-q", "rich==13.9.4", check=False).returncode == 0:
        return
    say("Could not install 'rich'; the mc-hub command will use plain text. Everything else works.")


UNIT_TEXT = """[Unit]
Description=CraftShare hub (Minecraft invites)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/mc-hub/mc_hub.py serve
Restart=always
RestartSec=3
# resource caps: a flood cannot take the VPS down
MemoryMax=64M
TasksMax=64
CPUQuota=25%
# sandbox: no capabilities, read-only system, may only write its own state and the two key folders
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/mc-hub /home/mctunnel/.ssh /home/mcsync/.ssh
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
SystemCallFilter=@system-service
UMask=0077

[Install]
WantedBy=multi-user.target
"""

WRAPPER = """#!/bin/sh
# CraftShare hub command. Run: mc-hub help
PY=/usr/bin/python3
[ -x /opt/mc-hub/venv/bin/python ] && PY=/opt/mc-hub/venv/bin/python
exec "$PY" /opt/mc-hub/mc_hub.py "$@"
"""


def setup_service(_args=None) -> None:
    """(Re)write the program, unit and command, then restart. Used by install and update."""
    require_root()
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    target = INSTALL_DIR / "mc_hub.py"
    if not target.exists() or Path(__file__).resolve() != target.resolve():
        shutil.copy(Path(__file__).resolve(), target)
    os.chmod(target, 0o644)
    for p in (STATE_DIR, *(a.parent for a in AUTH.values())):
        p.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(UNIT_TEXT)
    BIN.write_text(WRAPPER)
    os.chmod(BIN, 0o755)
    sh("systemctl", "daemon-reload")
    sh("systemctl", "enable", "mc-hub")
    sh("systemctl", "restart", "mc-hub")


def install(firewall: bool) -> None:
    require_root()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    nologin = shutil.which("nologin") or "/usr/sbin/nologin"
    with locked():
        st = load()
        inst = st["installed"]
        for user in USERS:
            if sh("id", user, check=False).returncode:
                sh("useradd", "-m", "-s", nologin, user)
            sh("usermod", "-p", "*", user)                    # key login only, no password possible
            sh("install", "-d", "-o", "root", "-g", "root", "-m", "755", f"/home/{user}/.ssh")
            AUTH[user].touch()
            os.chmod(AUTH[user], 0o644)
        sh("install", "-d", "-o", "mcsync", "-g", "mcsync", "-m", "700", str(WORLD_DIR))
        if not gateway_set():
            with open(SSHD_CONFIG, "a") as f:
                f.write(f"\n{GATEWAY_MARK}\nGatewayPorts clientspecified\n")
            inst["gateway_added"] = True
        reload_sshd()
        if firewall:
            inst["firewall"] += [r for r in open_firewall() if r not in inst["firewall"]]
        else:
            say(f"Firewall left unchanged. Friends need TCP {MC_PORT} (game) and {HUB_PORT} (invites) open.")
        install_rich(st)
        make_cert()
        first = not st["admin"]
        token = make_token(st) if first else ""
        code = code_for(st, token) if first else ""
        save(st)
    setup_service()
    if not healthy():
        say("[red]The service did not start. See: journalctl -u mc-hub[/]")
    say("\n[green]CraftShare hub is running.[/] Existing world files and keys were not touched.")
    if first:
        panel("YOUR OWNER CODE (paste it into the app, keep it private)", [f"[bold green]{code}[/]"])
    else:
        say("(An owner code already exists. Make a new one with: mc-hub owner-code)")
    say("Manage it any time with the command:  [bold]mc-hub[/]   (mc-hub help for all commands)")


# ---- uninstall
def make_export() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EXPORT_DIR, 0o700)
    for old in EXPORT_DIR.glob("*.zip"):
        old.unlink()
    out = EXPORT_DIR / f"craftshare-world-backup-{time.strftime('%Y%m%d-%H%M')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:          # world zips are compressed already
        for p in sorted(WORLD_DIR.rglob("*")):
            if p.is_file() and "lock" not in p.relative_to(WORLD_DIR).parts:
                z.write(p, p.relative_to(WORLD_DIR).as_posix())
    os.chmod(out, 0o600)
    return out


def offer_backup(st: dict) -> bool:
    """Optional download of the shared world before it is deleted. True to go on with the uninstall."""
    files = [p for p in WORLD_DIR.rglob("*") if p.is_file()] if WORLD_DIR.exists() else []
    if not files:
        return True
    if not ask_yes("Download a backup of the shared world before it is deleted? (recommended)", True):
        return ask_yes("[red]Delete the shared world WITHOUT a backup? This cannot be undone.[/]", False)
    if service_state() != "active":
        sh("systemctl", "start", "mc-hub", check=False)
    path = make_export()
    token = secrets.token_urlsafe(32)
    with locked():
        st = load()
        st["export"] = {"hash": sha(token), "file": path.name, "exp": time.time() + EXPORT_TTL,
                        "left": EXPORT_DOWNLOADS, "downloads": 0}
        save(st)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ip = public_ip(st)
    url = f"https://{ip}:{HUB_PORT}/export/{token}"
    panel("World backup", [
        f"Size     {size_text(path.stat().st_size)}",
        f"SHA-256  {digest}",
        "",
        "[bold]Download in a browser[/] (works for 1 hour, at most 3 downloads):",
        f"[bold green]{url}[/]",
        "Your browser warns about the certificate because it is self-signed: choose Advanced > Continue.",
        "",
        "[bold]Or copy it over SSH[/] (encrypted, from your PC):",
        f"scp root@{ip}:{path} .",
    ])
    while True:
        ans = ask_text("Type [bold]done[/] after you have downloaded it, or [bold]skip[/] to go on without it").strip().lower()
        downloads = (load().get("export") or {}).get("downloads", 0)
        if ans == "done":
            if downloads == 0 and not ask_yes("The link was not used yet (fine if you used scp). Do you have the backup?", False):
                continue
            return True
        if ans == "skip":
            return ask_yes("[red]Delete the shared world without keeping the backup?[/]", False)


def cmd_uninstall(_args) -> None:
    require_root()
    st = load()
    inst = st["installed"]
    world_files = [p for p in WORLD_DIR.rglob("*") if p.is_file()] if WORLD_DIR.exists() else []
    world_size = sum(p.stat().st_size for p in world_files)
    gw_line = "added by the hub: removed" if inst["gateway_added"] else \
        ("set before the hub: you will be asked" if gateway_set() else "not set")
    rows = [["Service and command", "mc-hub service, /opt/mc-hub, /usr/local/bin/mc-hub"],
            ["Hub data", "/var/lib/mc-hub (friends, owner code, certificate)"],
            ["Accounts", "mctunnel, mcsync and their home folders (all friend keys)"],
            ["Shared world", f"/srv/mcworld: {len(world_files)} files, {size_text(world_size)}"],
            ["Firewall rules", ", ".join(f"{r[0]} {r[1]} {r[2]}" for r in inst["firewall"]) or "none were added"],
            ["sshd GatewayPorts", gw_line]]
    if inst["pkg_rich"]:
        rows.append(["Package", f"python3-rich ({inst['pkg_rich']})"])
    table("This will remove", ["What", "Details"], rows)
    say("3x-ui / Xray and anything else on this server are not touched.")
    if not ask_yes("Uninstall the CraftShare hub and delete everything listed above?", False):
        say("Nothing was changed.")
        return
    if not offer_backup(st):
        say("Nothing was changed.")
        return
    if ask_text("Last check. Type [bold red]DELETE[/] to remove everything").strip() != "DELETE":
        say("Nothing was changed.")
        return

    say("\nRemoving...")
    sh("systemctl", "disable", "--now", "mc-hub", check=False)
    UNIT.unlink(missing_ok=True)
    sh("systemctl", "daemon-reload", check=False)
    say("  service stopped and removed")
    close_firewall(inst["firewall"])
    lines = SSHD_CONFIG.read_text().splitlines()
    backup = SSHD_CONFIG.with_name("sshd_config.mc-hub-bak")
    if inst["gateway_added"]:
        new = [ln for i, ln in enumerate(lines) if ln != GATEWAY_MARK and
               not (ln.strip() == "GatewayPorts clientspecified" and i > 0 and lines[i - 1] == GATEWAY_MARK)]
    elif gateway_set() and ask_yes("GatewayPorts was set before the hub was installed (by the first CraftShare setup). "
                                   "Remove it too? Say no if other tunnels on this server need it.", False):
        new = [ln for ln in lines if not re.match(r"^\s*GatewayPorts\s+(yes|clientspecified)\s*$", ln, re.I)]
    else:
        new = lines
    if new != lines:
        shutil.copy2(SSHD_CONFIG, backup)
        SSHD_CONFIG.write_text("\n".join(new) + "\n")
        if sh("sshd", "-t", check=False).returncode:
            shutil.copy2(backup, SSHD_CONFIG)
            say("  [red]sshd config test failed; GatewayPorts left as it was[/]")
        else:
            reload_sshd()
            say("  GatewayPorts removed from sshd")
        backup.unlink(missing_ok=True)
    for user in USERS:
        if sh("id", user, check=False).returncode == 0:
            sh("pkill", "-u", user, check=False)             # ends open tunnels / sftp sessions
            time.sleep(0.5)
            sh("userdel", "-r", user, check=False)
            say(f"  account {user} removed")
    for path in (WORLD_DIR, STATE_DIR, INSTALL_DIR):
        shutil.rmtree(path, ignore_errors=True)
    BIN.unlink(missing_ok=True)
    say("  files removed: /srv/mcworld, /var/lib/mc-hub, /opt/mc-hub, /usr/local/bin/mc-hub")
    if inst["pkg_rich"] == "apt-get":
        sh("apt-get", "remove", "-y", "-qq", "python3-rich", check=False)
    elif inst["pkg_rich"] == "dnf":
        sh("dnf", "remove", "-y", "-q", "python3-rich", check=False)
    say("\n[green]CraftShare hub is uninstalled.[/]")


def cmd_help(_args) -> None:
    print(__doc__)


COMMANDS = {"status": cmd_status, "invite": cmd_invite, "friends": cmd_friends, "remove": cmd_remove,
            "remove-legacy": cmd_remove_legacy, "owner-code": cmd_owner_code, "logs": cmd_logs,
            "update": cmd_update, "uninstall": cmd_uninstall, "require-app": cmd_require_app, "setup-service": setup_service,
            "help": cmd_help, "--help": cmd_help, "-h": cmd_help}


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "serve":
        return serve()
    if cmd == "install":
        if "--firewall" in args:
            fw = True
        elif "--no-firewall" in args or not sys.stdin.isatty():
            fw = False
        else:
            fw = ask_yes(f"Open TCP {MC_PORT} and {HUB_PORT} in this server's firewall?", False)
        return install(fw)
    if cmd not in COMMANDS:
        raise SystemExit(f"Unknown command '{cmd}'. Run: mc-hub help")
    try:
        COMMANDS[cmd](args[1:])
    except KeyboardInterrupt:
        say("\nCancelled. Nothing more was changed.")


if __name__ == "__main__":
    main()
