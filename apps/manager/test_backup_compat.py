"""Backup compatibility test: python apps/manager/test_backup_compat.py

Builds backups in every shape older versions produced (world only, partial settings, damaged files, world at
the zip root) plus a fresh round trip, and checks each restores without breaking. Uses a throwaway data folder.
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ["MCMGR_DATA"] = str(TMP / "data")
os.environ["MCMGR_CONFIG"] = str(TMP / "data" / "cfg.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import world  # noqa: E402

SERVER = core.SERVER_DIR
OPS = [{"uuid": "11111111-1111-1111-1111-111111111111", "name": "Helitha", "level": 4, "bypassesPlayerLimit": False}]
WL = [{"uuid": "22222222-2222-2222-2222-222222222222", "name": "Dulaj"}]


def reset(props: str = "level-name=world\nserver-port=25565\ngamemode=survival\nextra-local-key=keepme\n") -> None:
    import shutil
    shutil.rmtree(SERVER, ignore_errors=True)
    core.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for old in core.BACKUP_DIR.glob("*.zip"):
        old.unlink()
    (SERVER / "world" / "players" / "data").mkdir(parents=True)
    (SERVER / "world" / "level.dat").write_bytes(b"LEVEL-CURRENT")
    (SERVER / "world" / "players" / "data" / "a.dat").write_bytes(b"PLAYER-CURRENT")
    (SERVER / "server.properties").write_text(props)
    (SERVER / "ops.json").write_text(json.dumps([]))
    (SERVER / "whitelist.json").write_text(json.dumps([]))


def make_zip(name: str, files: dict[str, bytes | str], folder: str = "world") -> Path:
    p = TMP / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"{folder + '/' if folder else ''}level.dat", b"LEVEL-BACKUP")
        z.writestr(f"{folder + '/' if folder else ''}players/data/a.dat", b"PLAYER-BACKUP")
        for n, c in files.items():
            z.writestr(n, c)
    return p


def world_is_backup() -> bool:
    return (SERVER / "world" / "level.dat").read_bytes() == b"LEVEL-BACKUP" and \
        (SERVER / "world" / "players" / "data" / "a.dat").read_bytes() == b"PLAYER-BACKUP"


def props() -> dict:
    return core.read_properties(SERVER / "server.properties")


# 1. oldest format: world only, no _server folder
reset()
r = world.restore_world(make_zip("old1.zip", {}), include_config=True)
assert world_is_backup() and r == {"config_restored": [], "config_skipped": []}, r
assert props()["gamemode"] == "survival" and not world.backup_has_config(TMP / "old1.zip")
print("ok  world-only backup restores, current settings untouched")

# 2. partial _server (before bans/usercache existed): only what is present is applied
reset()
z = make_zip("old2.zip", {"_server/server.properties": "gamemode=creative\nmax-players=5\n",
                          "_server/ops.json": json.dumps(OPS)})
r = world.restore_world(z, include_config=True)
assert world_is_backup() and sorted(r["config_restored"]) == ["ops.json", "server.properties"], r
assert json.loads((SERVER / "ops.json").read_text()) == OPS and (SERVER / "whitelist.json").read_text() == "[]"
assert props()["gamemode"] == "creative" and props()["max-players"] == "5" and props()["extra-local-key"] == "keepme"
print("ok  partial settings backup: restored what exists, kept the rest")

# 3. settings that belong to this PC are never taken from a backup
reset()
z = make_zip("old3.zip", {"_server/server.properties": "level-name=otherworld\nserver-port=1234\nserver-ip=9.9.9.9\ndifficulty=hard\n"})
world.restore_world(z, include_config=True)
p = props()
assert p["level-name"] == "world" and p["server-port"] == "25565" and "server-ip" not in p and p["difficulty"] == "hard", p
assert world.has_world()
print("ok  level-name / port / bind address stay local; the world is still found")

# 4. damaged and wrongly-shaped files are skipped, everything else still restores
reset()
z = make_zip("old4.zip", {"_server/ops.json": "{not json", "_server/whitelist.json": json.dumps({"a": 1}),
                          "_server/banned-players.json": json.dumps([{"name": "x"}]),
                          "_server/usercache.json": json.dumps([{"uuid": "u", "name": "n", "expiresOn": "x"}]),
                          "_server/server.properties": "pvp=false\n"})
r = world.restore_world(z, include_config=True)
assert world_is_backup() and sorted(r["config_skipped"]) == ["banned-players.json", "ops.json", "whitelist.json"], r
assert sorted(r["config_restored"]) == ["server.properties", "usercache.json"], r
assert (SERVER / "ops.json").read_text() == "[]", "damaged file must not replace the current one"
print("ok  damaged files skipped, world and good files restored")

# 5. world at the zip root (hand-made zip) still restores
reset()
r = world.restore_world(make_zip("old5.zip", {}, folder=""), include_config=True)
assert world_is_backup()
print("ok  zip with the world at its root restores")

# 6. a zip someone else gave you never restores settings, even if asked (the web layer enforces is_app_backup)
for name, trusted in [("auto-20260101-000000.zip", True), ("manual-1.zip", True), ("sync-1.zip", True),
                      ("pre-import-1.zip", True), ("vps-world-3-20260101-000000.zip", True),
                      ("cool-world-from-reddit.zip", False), ("world.zip", False)]:
    assert world.is_app_backup(Path(name)) is trusted, name
print("ok  only the app's own backups count as trusted")

# 7. fresh round trip: back up, change settings and player lists, restore everything
reset()
(SERVER / "ops.json").write_text(json.dumps(OPS))
(SERVER / "whitelist.json").write_text(json.dumps(WL))
(SERVER / "usercache.json").write_text(json.dumps([{"uuid": "u", "name": "n", "expiresOn": "x"}]))
(SERVER / "server.properties").write_text("level-name=world\nserver-port=25565\ngamemode=creative\ndifficulty=hard\n")
out = world.backup_world("manual")
assert out and world.backup_has_config(out) and world.is_app_backup(out)
names = zipfile.ZipFile(out).namelist()
assert "world/players/data/a.dat" in names and "_server/ops.json" in names and "_server/whitelist.json" in names, names
(SERVER / "ops.json").write_text("[]")
(SERVER / "whitelist.json").write_text("[]")
(SERVER / "server.properties").write_text("level-name=world\nserver-port=25565\ngamemode=survival\ndifficulty=easy\n")
(SERVER / "world" / "players" / "data" / "a.dat").write_bytes(b"CHANGED")
r = world.restore_world(out, include_config=True)
assert sorted(r["config_restored"]) == ["ops.json", "server.properties", "usercache.json", "whitelist.json"], r
assert json.loads((SERVER / "ops.json").read_text()) == OPS and json.loads((SERVER / "whitelist.json").read_text()) == WL
assert props()["gamemode"] == "creative" and props()["difficulty"] == "hard"
assert (SERVER / "world" / "players" / "data" / "a.dat").read_bytes() == b"PLAYER-CURRENT"
print("ok  fresh backup round trip: world, player data, settings, ops and whitelist all come back")

# 8. world-only restore of a backup that has settings leaves the current settings alone
(SERVER / "ops.json").write_text("[]")
r = world.restore_world(out, include_config=False)
assert r["config_restored"] == [] and (SERVER / "ops.json").read_text() == "[]"
print("ok  'World only' leaves settings and player lists alone")

# 9. the duplicate check still sees an unchanged world as unchanged, and a settings-only change as new
before = len(world.list_backups())
assert world.backup_world("auto") is not None
reset()
(SERVER / "server.properties").write_text("level-name=world\ngamemode=survival\n")
first = world.backup_world("auto")
again = world.backup_world("auto")
assert first == again, "nothing changed: no duplicate"
(SERVER / "server.properties").write_text("level-name=world\ngamemode=creative\n")
st = (SERVER / "server.properties").stat()
os.utime(SERVER / "server.properties", ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))   # seconds later, like real life
third = world.backup_world("auto")
assert third != first, "a settings-only change must produce a new backup"
print("ok  duplicate check: unchanged = skipped, settings-only change = new backup")
print("backup compatibility tests passed")
