"""Build the zip to send to a friend:  python package.py [--exe]

The zip holds a top-level folder with the app (or the built .exe), the restricted tunnel and
sync keys, and a defaults.json that pre-fills the VPS settings. It never includes your own
SSH key, your world, or your saved settings.
"""
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
SOURCE_FILES = ["app.py", "core.py", "world.py", "web/index.html", "start.bat", "requirements.txt", "README.md"]
KEYS = ["keys/friend_tunnel", "keys/sync_key"]   # the only private keys that ever leave this PC


def main() -> None:
    if not DEFAULTS["ssh_host"]:
        sys.exit("Set VPS_HOST in .env first (copy .env.example to .env).")
    use_exe = "--exe" in sys.argv
    files = [("dist/MinecraftManager.exe", "MinecraftManager.exe")] if use_exe else [(f, f) for f in SOURCE_FILES]
    out = HERE / "MinecraftServerManager.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, name in files:
            z.write(HERE / src, f"{TOP}/{name}")
        for key in KEYS:
            z.write(HERE / key, f"{TOP}/{key}")
        z.writestr(f"{TOP}/defaults.json", json.dumps(DEFAULTS, indent=2))
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    for n in zipfile.ZipFile(out).namelist():
        print("  ", n)


if __name__ == "__main__":
    main()
