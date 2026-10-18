# Setting up your VPS

You need a Linux VPS where you can log in as root. This does not touch 3x-ui/Xray or other software.

## Install

```bash
bash <(curl -Ls https://raw.githubusercontent.com/iamhelitha/craftshare/main/install.sh)
```

It downloads `mc_hub.py` from the latest release, checks it against the release's `SHA256SUMS.txt`, and installs:
- `mc-hub`, a small HTTPS service on port 8766 (about 20 MB of RAM, sandboxed, rate-limited)
- two locked-down accounts: `mctunnel` (can only open port 25565) and `mcsync` (can only use sftp inside `/srv/mcworld`)
- the `mc-hub` command for managing it

It **asks** whether to open TCP **25565** (the game) and **8766** (invites) in the server's firewall (ufw or
firewalld), and only records the rules it adds itself, so uninstall can take exactly those back out. Skip the
question with `MC_FIREWALL=yes` or `MC_FIREWALL=no` before the command. If your provider also has a firewall in
its web panel, open the same two ports there; a script on the server cannot change that one.

At the end it prints your **owner code**. In the app: **Setup > Friends & access > I own the VPS**, paste it,
then press **Invite a friend**.

## The `mc-hub` command

| Command | What it does |
|---|---|
| `mc-hub` | Status: service, version and available update, friends, open invites, the shared world files, who is hosting now, blocked IPs and failed attempts |
| `mc-hub invite` | Make an invite code (works once, for 24 hours) |
| `mc-hub friends` / `mc-hub remove NAME` | List friends / remove one (asks first) |
| `mc-hub remove-legacy` | Remove the old shared keys from the first setup |
| `mc-hub owner-code` | Make a new owner code; the old one stops working |
| `mc-hub logs` | Recent service log |
| `mc-hub require-app 0.4.0` | Only app version 0.4.0 or newer may host (older apps are asked to update first); `off` removes the rule |
| `mc-hub update` | Update to the latest release (or `mc-hub update v0.4.0`) |
| `mc-hub uninstall` | Remove everything, after offering a world backup download |

## Updating

```bash
mc-hub update
```

1. Downloads `mc_hub.py` and `SHA256SUMS.txt` of the release over HTTPS from GitHub.
2. Refuses to continue if the checksum or the version inside the file does not match, or it does not compile.
3. Shows the versions and asks before changing anything.
4. Keeps the current file as `/opt/mc-hub/mc_hub.py.bak`, swaps in the new one and restarts the service.
5. Checks the service answers. If it does not, it puts the previous version back automatically.

Friends, invites, the owner code and the world are kept. Running the install one-liner again also updates.

## Uninstalling

```bash
mc-hub uninstall
```

It lists everything it will remove and asks for confirmation. Then, if there is a shared world, it offers a
**backup download** (optional):
- a link `https://<your-vps>:8766/export/<secret>` that works for 1 hour and at most 3 downloads (your browser
  warns about the self-signed certificate: choose Advanced > Continue), or
- an `scp` command to copy it over SSH instead.

The SHA-256 of the backup is shown so you can check the download. Type `done` when you have it (or `skip`), then
type `DELETE` to confirm. It removes the service, `/opt/mc-hub`, `/var/lib/mc-hub`, `/srv/mcworld`, the
`mctunnel`/`mcsync` accounts (closing any open tunnel), the firewall rules it added, and the `GatewayPorts`
line if it added it (if an older setup added it, you are asked). Nothing else on the server is touched.

The backup zip contains the `world-N.zip` files and `meta.json`. Import a `world-N.zip` in the app on the
Backups page.
