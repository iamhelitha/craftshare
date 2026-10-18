# Minecraft Server Manager

Double-click `start.bat` (or `MinecraftManager.exe` in the friend zip). A small window opens and your browser shows
the manager at `http://127.0.0.1:8765` (this PC only). To turn everything off, close that window or press
**Shut down** in the page: the server is stopped, the world saved, backed up and uploaded before it turns off.
That finishes in the background even after the window is gone, so give it a minute before shutting down the PC.

- **Home**: type your name, accept the EULA once, press **Start server**. Minecraft and Java download
  automatically the first time. With the shared world on, only one person can host at a time; the page shows who.
- **Players**: make admins, change game mode, heal, kick, ban, whitelist.
- **World settings**: game mode, difficulty, PvP, whitelist, cracked players, and so on.
- **Backups**: a backup is made at every start. Back up now, download, restore, or drop in any world `.zip`.
- **Console**: server log, commands, chat, quick buttons.
- **Setup**: Minecraft version, memory, Java, and (advanced) the VPS connection.

Extract the zip first. Running it from inside the zip loses your settings.
Data (settings, world, backups, Java) lives in the folder shown on the Setup page.

For the owner: `vps_setup.sh` (run once as root) creates the tunnel-only and storage-only VPS accounts.
`python package.py` builds the friend zip; `build.bat` builds the .exe version.
