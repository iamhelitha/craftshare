"""World backup/restore and the VPS-backed world store with a host lock.

The VPS only stores files (one lock, one heartbeat, versioned world zips) through an
sftp-only account; nobody's PC is trusted to be online, so the lock decides who hosts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import core

CONFIG_FILES = ("server.properties", "ops.json", "whitelist.json",
                "banned-players.json", "banned-ips.json", "usercache.json")
CONFIG_PREFIX = "_server/"        # where those files live inside a backup zip
STATE_FILE = core.DATA_DIR / "sync_state.json"
STALE_AFTER = 180                 # seconds without a heartbeat before a lock counts as dead
AUTOMATIC = ("auto", "sync", "pre-import")   # made by the app; may be deleted to save space
# Retention, set by the app from the user's settings (see set_retention).
RETENTION = {"keep_auto": 5, "max_bytes": 5 * 1024 ** 3}


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- local backups
def level_name() -> str:
    return core.read_properties(core.SERVER_DIR / "server.properties").get("level-name", "world") or "world"


def has_world() -> bool:
    return (core.SERVER_DIR / level_name() / "level.dat").exists()


def _fingerprint(world: Path) -> str:
    """Cheap identity of the world's current state (paths, sizes, modification times)."""
    h = hashlib.sha1()
    files = [p for p in world.rglob("*") if p.is_file() and p.name != "session.lock"]
    files += [core.SERVER_DIR / n for n in CONFIG_FILES if (core.SERVER_DIR / n).exists()]
    for p in sorted(files):
        st = p.stat()
        h.update(f"{p.as_posix()}|{st.st_size}|{st.st_mtime_ns};".encode())
    return h.hexdigest()


def _zip_fingerprint(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            return z.comment.decode(errors="replace")
    except (OSError, zipfile.BadZipFile):
        return ""


def list_backups() -> list[Path]:
    """Newest first."""
    if not core.BACKUP_DIR.exists():
        return []
    return sorted(core.BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


def backup_world(tag: str = "manual") -> Path | None:
    """Zip the world (+ properties/ops/whitelist) into DATA_DIR/backups. None if there is no world yet.

    If the newest backup already holds exactly this state, that file is returned instead of
    writing a duplicate (e.g. the auto-backup right after a stop-and-upload).
    """
    world = core.SERVER_DIR / level_name()
    if not (world / "level.dat").exists():
        return None
    fp = _fingerprint(world)
    existing = list_backups()
    if existing and _zip_fingerprint(existing[0]) == fp:
        return existing[0]
    core.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = core.BACKUP_DIR / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.zip"
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in world.rglob("*"):
            if p.is_file() and p.name != "session.lock":
                z.write(p, f"{world.name}/{p.relative_to(world).as_posix()}")
        for name in CONFIG_FILES:
            f = core.SERVER_DIR / name
            if f.exists():
                z.write(f, CONFIG_PREFIX + name)
        z.comment = fp.encode()
    os.replace(tmp, out)
    prune_backups()
    return out


def set_retention(keep_auto: int, max_gb: float) -> None:
    RETENTION["keep_auto"] = max(1, int(keep_auto))
    RETENTION["max_bytes"] = int(max(0.5, float(max_gb)) * 1024 ** 3)


def prune_backups() -> list[str]:
    """Apply the retention rules; returns the names deleted. Backups you made yourself are never touched.

    - automatic start backups: keep the newest `keep_auto`
    - upload copies: keep only the newest (the VPS keeps its own 3 versions)
    - safety copies taken before a restore: keep the newest 3
    - then, while everything together is over the space limit, delete the oldest automatic backup,
      but always keep the single newest backup of any kind.
    """
    deleted = []
    if not core.BACKUP_DIR.exists():
        return deleted

    def kind(p: Path) -> str:
        return p.name.split("-")[0]

    limits = {"auto": RETENTION["keep_auto"], "sync": 1, "pre-import": 3}
    for tag, keep in limits.items():
        for old in [p for p in list_backups() if kind(p) == tag][keep:]:
            old.unlink(missing_ok=True)
            deleted.append(old.name)
    files = list_backups()
    total = sum(p.stat().st_size for p in files)
    newest = files[0] if files else None
    for old in reversed(files):                          # oldest first
        if total <= RETENTION["max_bytes"]:
            break
        if old is newest or kind(old) not in AUTOMATIC:
            continue
        total -= old.stat().st_size
        old.unlink(missing_ok=True)
        deleted.append(old.name)
    return deleted


def delete_backup(name: str) -> bool:
    p = (core.BACKUP_DIR / name).resolve()
    if p.parent != core.BACKUP_DIR.resolve() or p.suffix != ".zip" or not p.exists():
        return False
    p.unlink()
    return True


def restore_world(zip_path: Path, include_config: bool = False) -> None:
    """Replace the current world with the one in `zip_path` (current one is backed up first)."""
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("That file is not a zip archive.")
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="mcrestore-", dir=core.DATA_DIR))
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)             # extractall strips absolute paths and '..'
        cfg_name = CONFIG_PREFIX.strip("/")
        found = [p.parent for p in tmp.rglob("level.dat") if cfg_name not in p.relative_to(tmp).parts]
        if not found:
            raise RuntimeError("No Minecraft world found in that zip (no level.dat).")
        src = min(found, key=lambda p: len(p.parts))
        dest = core.SERVER_DIR / level_name()
        backup_world("pre-import")
        core.SERVER_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
        cfg_dir = tmp / cfg_name
        if include_config and cfg_dir.is_dir():
            for f in cfg_dir.iterdir():
                if f.name in CONFIG_FILES:
                    shutil.copy2(f, core.SERVER_DIR / f.name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- sftp plumbing
def _run_sftp(cfg: dict, commands: list[str], workdir: Path) -> subprocess.CompletedProcess:
    """Run sftp in batch mode. Tests replace this with a local fake."""
    exe = core.find_tool("sftp")
    if not exe:
        raise SyncError("sftp not found - install the OpenSSH client.")
    batch = workdir / f"batch-{uuid.uuid4().hex[:8]}.txt"
    batch.write_text("\n".join(commands) + "\n", encoding="utf-8")
    args = [exe, "-b", str(batch), "-P", str(cfg["ssh_port"]), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    shipped = core.BASE / "keys" / "sync_key"
    key_path = cfg["sync_key"] or (str(shipped) if shipped.exists() else cfg["ssh_key"])
    if key_path:
        args += ["-i", core.usable_key(key_path)]
    args += core.ssh_proxy_args(cfg)
    args.append(f"{cfg['sync_user']}@{cfg['ssh_host']}")
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise SyncError("Connection to the VPS timed out.")
    finally:
        batch.unlink(missing_ok=True)


def _q(p: Path | str) -> str:
    return '"' + str(p).replace("\\", "/") + '"'


def _err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or res.stdout).strip()[-200:]


class _Remote:
    """sftp batches plus a scratch dir for local files."""

    def __init__(self, cfg: dict):
        if not (cfg.get("ssh_host") and cfg.get("sync_user")):
            raise SyncError("Sync is not configured (VPS host / sync user missing).")
        self.cfg = cfg
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="mcsync-", dir=core.DATA_DIR))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self, commands: list[str]) -> subprocess.CompletedProcess:
        return _run_sftp(self.cfg, commands, self.tmp)

    def get_json(self, name: str) -> dict | None:
        local = self.tmp / f"get-{uuid.uuid4().hex[:6]}.json"
        r = self.run([f"get {name} {_q(local)}"])
        if r.returncode != 0:
            out = (r.stdout + r.stderr).lower()
            if "no such file" in out or "not found" in out or "couldn't stat" in out:
                return None
            raise SyncError(f"Cannot reach the world store: {_err(r)}")
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put_json(self, name: str, data: dict) -> None:
        """Overwrite `name` with a plain put (fine for heartbeat; readers tolerate a stale copy)."""
        local = self.tmp / ("put-" + name.replace("/", "_"))
        local.write_text(json.dumps(data), encoding="utf-8")
        r = self.run([f"put {_q(local)} {name}"])
        if r.returncode != 0:
            raise SyncError(f"Upload of {name} failed: {_err(r)}")


# ---------------------------------------------------------------- lock + status
# The lock is the directory `lock/`: sftp mkdir fails if it exists, so exactly one caller wins.
# (sftp rename silently overwrites on real servers, so it cannot be used for this.)
def _lock_exists(r: "_Remote") -> bool:
    res = r.run(["ls lock"])
    if res.returncode == 0:
        return True
    out = (res.stdout + res.stderr).lower()
    if "not found" in out or "no such file" in out:
        return False
    raise SyncError(f"Cannot reach the world store: {_err(res)}")


def host_status(cfg: dict) -> dict:
    """{'state': free|running|stale, 'holder', 'since', 'lock_id', 'port_open'}"""
    with _Remote(cfg) as r:
        if not _lock_exists(r):
            lock = hb = None
        else:
            lock = r.get_json("lock/lock.json") or {}
            hb = r.get_json("lock/heartbeat.json") or {}
    port_open = _port_open(cfg["ssh_host"], int(cfg["ssh_remote_port"] or 25565))
    if lock is None:
        return {"state": "free", "port_open": port_open}
    beat = hb.get("time") or lock.get("since") or 0
    stale = time.time() - beat > STALE_AFTER
    return {"state": "stale" if stale else "running", "holder": lock.get("holder", "?"),
            "since": lock.get("since", 0), "lock_id": lock.get("id"), "port_open": port_open}


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(s: socket.socket) -> int:
    n = shift = 0
    while True:
        b = s.recv(1)
        if not b:
            raise OSError("connection closed")
        n |= (b[0] & 0x7F) << shift
        shift += 7
        if not b[0] & 0x80:
            return n


def mc_ping(host: str, port: int, proxy: tuple[str, int] | None = None, timeout: float = 5.0) -> dict | None:
    """Ask a Minecraft server for its status, the way the multiplayer list does.

    Only a running, fully started server answers, so this tells "friends can join now" apart from
    "port is open but the server is still starting". Returns None when there is no answer."""
    try:
        s = core.socks_connect(host, port, proxy, timeout) if proxy else socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        with s:
            hs = _varint(0) + _varint(767) + _varint(len(host)) + host.encode() + struct.pack(">H", port) + _varint(1)
            s.sendall(_varint(len(hs)) + hs + _varint(1) + _varint(0))
            _read_varint(s)
            _read_varint(s)
            n = _read_varint(s)
            data = b""
            while len(data) < n:
                chunk = s.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
            st = json.loads(data)
            return {"players": st.get("players", {}).get("online", 0), "max": st.get("players", {}).get("max", 0),
                    "version": st.get("version", {}).get("name", "")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def acquire_lock(cfg: dict, name: str, my_id: str) -> tuple[bool, dict | None]:
    """Atomically take the host lock (mkdir). Returns (True, None) or (False, current holder info)."""
    now = time.time()
    with _Remote(cfg) as r:
        res = r.run(["mkdir lock"])
        if res.returncode != 0:
            if not _lock_exists(r):                  # failed for another reason (or VPS unreachable)
                raise SyncError(f"Could not take the lock: {_err(res)}")
            return False, r.get_json("lock/lock.json") or {"holder": "?"}
        r.put_json("lock/lock.json", {"id": my_id, "holder": name, "since": now})
        r.put_json("lock/heartbeat.json", {"id": my_id, "holder": name, "time": now})
        return True, None


def heartbeat(cfg: dict, name: str, my_id: str) -> None:
    with _Remote(cfg) as r:
        cur = r.get_json("lock/lock.json")
        if cur is None or cur.get("id") != my_id:
            raise SyncError("You no longer hold the host lock.")
        r.put_json("lock/heartbeat.json", {"id": my_id, "holder": name, "time": time.time()})


def release_lock(cfg: dict, my_id: str, force: bool = False) -> None:
    with _Remote(cfg) as r:
        if not _lock_exists(r):
            return
        cur = r.get_json("lock/lock.json")
        if cur is not None and cur.get("id") != my_id and not force:
            raise SyncError("The lock now belongs to someone else; not releasing it.")
        r.run(["-rm lock/lock.json", "-rm lock/heartbeat.json", "rmdir lock"])


_LS_RE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(world-(\d+)\.zip)\s*$")


def list_remote_worlds(cfg: dict) -> list[dict]:
    """Every world version stored on the VPS: [{name, version, size, date, latest}], newest first."""
    with _Remote(cfg) as r:
        meta = r.get_json("meta.json") or {}
        res = r.run(["ls -l"])
        if res.returncode != 0:
            raise SyncError(f"Cannot list the shared storage: {_err(res)}")
    latest = meta.get("file", "")
    out = []
    for line in res.stdout.splitlines():
        m = _LS_RE.match(line.strip())
        if m:
            out.append({"name": m.group(3), "version": int(m.group(4)), "size": int(m.group(1)),
                        "date": m.group(2), "latest": m.group(3) == latest,
                        "by": meta.get("by", "") if m.group(3) == latest else ""})
    return sorted(out, key=lambda x: -x["version"])


def download_remote_world(cfg: dict, name: str, progress=lambda msg: None) -> Path:
    """Copy one VPS version into the local backups folder (it does not change the current world)."""
    if not re.fullmatch(r"world-\d+\.zip", name):
        raise SyncError("Bad file name.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"vps-{name[:-4]}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with _Remote(cfg) as r:
        progress(f"Downloading {name} from the VPS...")
        part = r.tmp / name
        res = r.run([f"get {name} {_q(part)}"])
        if res.returncode != 0 or not part.exists():
            raise SyncError(f"Download failed: {_err(res)}")
        shutil.move(str(part), dest)
    return dest


def delete_remote_world(cfg: dict, name: str) -> None:
    """Delete an old version on the VPS. The newest one (listed in meta.json) is never deleted."""
    if not re.fullmatch(r"world-\d+\.zip", name):
        raise SyncError("Bad file name.")
    with _Remote(cfg) as r:
        meta = r.get_json("meta.json") or {}
        if meta.get("file") == name:
            raise SyncError("That is the current shared world. Upload a newer one first, or restore a backup instead.")
        res = r.run([f"rm {name}"])
        if res.returncode != 0:
            raise SyncError(f"Delete failed: {_err(res)}")


def read_policy(cfg: dict) -> dict:
    """Rules the server owner set with `mc-hub require-app` (e.g. {"min_app": "0.4.0"}); {} if none."""
    with _Remote(cfg) as r:
        return r.get_json("policy.json") or {}


# ---------------------------------------------------------------- world transfer
def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "dirty": False}


def _save_state(**kw) -> None:
    s = _state()
    s.update(kw)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s), encoding="utf-8")


def is_dirty() -> bool:
    """True if the last hosted session never finished uploading its world."""
    return bool(_state().get("dirty"))


def mark_dirty() -> None:
    _save_state(dirty=True)


def check_remote(cfg: dict) -> dict:
    """What the VPS holds versus this PC. The UI shows this and lets the user decide to pull."""
    with _Remote(cfg) as r:
        meta = r.get_json("meta.json")
    st = _state()
    remote_v = (meta or {}).get("version", 0)
    return {"exists": bool(meta), "remote_version": remote_v, "local_version": st.get("version", 0),
            "by": (meta or {}).get("by", "?"), "time": (meta or {}).get("time", 0),
            "newer": bool(meta) and (remote_v > st.get("version", 0) or not has_world()),
            "dirty": bool(st.get("dirty")), "has_local_world": has_world()}


def pull_world(cfg: dict, progress=lambda msg: None) -> int:
    """Download the newest uploaded world (current one is backed up first). Returns its version."""
    with _Remote(cfg) as r:
        meta = r.get_json("meta.json")
        if not meta:
            raise SyncError("There is no world on the VPS yet.")
        progress(f"Downloading world version {meta['version']}...")
        local = r.tmp / "world.zip"
        res = r.run([f"get {meta['file']} {_q(local)}"])
        if res.returncode != 0:
            raise SyncError(f"World download failed: {_err(res)}")
        restore_world(local, include_config=True)
    _save_state(version=meta["version"], dirty=False)
    return meta["version"]


def push_world(cfg: dict, name: str, progress=lambda msg: None) -> int:
    """Upload the local world as the next version. Local data is never deleted on failure."""
    zip_path = backup_world("sync")
    if not zip_path:
        return _state().get("version", 0)       # no world generated yet: nothing to upload
    with _Remote(cfg) as r:
        meta = r.get_json("meta.json") or {}
        new_v = max(meta.get("version", 0), _state().get("version", 0)) + 1
        remote_name = f"world-{new_v}.zip"
        progress(f"Uploading world ({zip_path.stat().st_size // (1024 * 1024)} MB)...")
        res = r.run([f"put {_q(zip_path)} {remote_name}.part", f"rename {remote_name}.part {remote_name}"])
        if res.returncode != 0:
            raise SyncError(f"World upload failed: {_err(res)}")
        local = r.tmp / "meta.local"
        local.write_text(json.dumps({"version": new_v, "file": remote_name, "by": name,
                                     "time": time.time()}), encoding="utf-8")
        res = r.run(["-rm meta.json", f"put {_q(local)} meta.json"])
        if res.returncode != 0:
            raise SyncError(f"Could not update the world index: {_err(res)}")
        old = [f"-rm world-{v}.zip" for v in range(1, new_v - 2)]   # keep the last 3 versions
        if old:
            r.run(old)
    _save_state(version=new_v, dirty=False)
    return new_v


def adopt_local(cfg: dict) -> None:
    """Make this PC's world the one to upload next (used after importing a rescued world)."""
    with _Remote(cfg) as r:
        remote_v = (r.get_json("meta.json") or {}).get("version", 0)
    _save_state(version=remote_v, dirty=True)
