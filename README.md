# CraftShare (Minecraft Server Manager)

Host a vanilla Minecraft server from your own Windows PC, let friends join through your VPS, and share one
world so anyone in the group can host when they want: one at a time, with the latest progress.

- **Friendly web page**: start/stop, players and admins, world settings, backups, console. No terminal.
- **No port forwarding**: an SSH reverse tunnel publishes the server on your VPS (`VPS:25565`).
- **Shared world**: kept on the VPS. Whoever hosts downloads the latest copy and uploads it when done. A lock
  stops two people hosting at once.
- **Invites, not shared passwords**: each PC makes its own key; the owner sends a single-use invite code.
- **No installs for friends**: one `.exe`; Minecraft and Java download automatically.
- Light on the VPS (about 20 MB of RAM) and does not touch other software such as 3x-ui/Xray.

## Download

Get `MinecraftManager.exe` (or the zip) from the [latest release](../../releases/latest).
Put it in its own folder and double-click it. Your browser opens the manager at `http://127.0.0.1:8765`
(this PC only). Close the black window or press **Shut down** to stop: the world is saved, backed up and
uploaded first.

## Owner: set up your VPS (once)

Log in to your Linux VPS as root and run:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/iamhelitha/craftshare/main/install.sh)
```

It prints an **owner code**. In the app open **Setup > Friends & access > I own the VPS** and paste it.
If your provider has a firewall panel, open TCP **25565** and **8766**. Details: [docs/vps-setup.md](docs/vps-setup.md).

## Friends: join

1. The owner presses **Invite a friend** and sends you the code.
2. Open the app, type your name in **Setup**, paste the code under **Join a server**, press **Join**.
3. Go to **Home** and press **Start server** to host, or just join the address shown there in Minecraft.

## The pages

- **Home**: start/stop hosting, who is hosting, the address to join, players online.
- **Players**: make admins, change game mode, heal, kick, ban, whitelist.
- **World settings**: game mode, difficulty, PvP, whitelist, cracked players and more.
- **Backups**: automatic at every start, with limits so they do not fill your disk. Restore or drop in any world `.zip`.
- **Console**: server log, commands, chat.
- **Setup**: your name, Minecraft version, memory, Java, network/proxy, friends and access.

## Run from source

```bat
start.bat
```

Needs Python 3.11+. Developer notes: [CONTRIBUTING.md](CONTRIBUTING.md), [docs/architecture.md](docs/architecture.md).

## Repository layout

```
apps/manager/   the Windows app (aiohttp backend + web/index.html)
apps/hub/       mc-hub, the VPS service for invites
install.sh      one-line VPS installer
docs/           setup and architecture notes
```

## License

[GPL-3.0](LICENSE). Not affiliated with Mojang or Microsoft.
