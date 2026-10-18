"""Build the zip to send to a friend:  python package.py [--exe]

The zip holds a top-level folder with the app (or the built .exe) and a minimal defaults.json. It
contains no keys and no server address: friends join with an invite from the owner (Setup > Friends
& access). `--with-keys` builds the old style zip with the shared keys. It never includes your own
SSH key, your world, or your saved settings.
"""
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent if (HERE.parent.parent / "VERSION").exists() else HERE   # repo root (README, VERSION)
TOP = "MinecraftServerManager"


def load_env() -> dict:
    """Read KEY=VALUE lines from .env (gitignored; see .env.example)."""
    env = {}
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
DEFAULTS = {
    "ssh_host": ENV.get("VPS_HOST", ""), "ssh_user": "mctunnel", "ssh_port": "22",
    "ssh_key": "keys/friend_tunnel", "ssh_remote_port": "25565", "ssh_bind": "0.0.0.0",
    "sync_enabled": True, "sync_user": "mcsync", "sync_key": "keys/sync_key",
    "version": "26.3",
}
SOURCE_FILES = ["app.py", "core.py", "world.py", "updater.py", "web/index.html", "start.bat", "requirements.txt"]
KEYS = ["keys/friend_tunnel", "keys/sync_key"]   # the only private keys that ever leave this PC


def main() -> None:
    legacy = "--with-keys" in sys.argv       # old style: ship the shared keys (not recommended)
    if legacy and not DEFAULTS["ssh_host"]:
        sys.exit("Set VPS_HOST in .env first (copy .env.example to .env).")
    use_exe = "--exe" in sys.argv
    files = [("dist/MinecraftManager.exe", "MinecraftManager.exe")] if use_exe else [(f, f) for f in SOURCE_FILES]
    out = HERE / "MinecraftServerManager.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, name in files:
            z.write(HERE / src, f"{TOP}/{name}")
        for extra in ("VERSION", "README.md"):
            if (ROOT / extra).exists():
                z.write(ROOT / extra, f"{TOP}/{extra}")
        if legacy:
            for key in KEYS:
                z.write(HERE / key, f"{TOP}/{key}")
            z.writestr(f"{TOP}/defaults.json", json.dumps(DEFAULTS, indent=2))
        else:                                # no secrets, no address: the friend joins with an invite
            z.writestr(f"{TOP}/defaults.json", json.dumps({"version": DEFAULTS["version"]}, indent=2))
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    for n in zipfile.ZipFile(out).namelist():
        print("  ", n)


if __name__ == "__main__":
    main()
